# AI Paper SP2 — Plan 5: ai controller runtime: RPC clients, leadership, schedule, submitter, outbox, signal intake, service, container — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the deterministic runtime of the `ai` container: two method-restricted typed RPC clients, trader-granted leadership, XNYS session slots, a persist-before-send submitter with reconciliation, an idempotent reporting outbox, a durable signal intake, one async controller that calls a `DecisionEngine` (implemented by Plan 6), the `trader/ai_service.py` entry point and the compose `ai` service. The controller makes no trading judgment.

**Architecture:** New modules in `trader/ai/` on top of Plan 4's `AiStore`, `AttemptJournal` and `ModelGateway`. Every trader call goes through `asyncio.to_thread` around SP1's blocking `TypedRpcClient`. `ai.duckdb` gets migrations 10–17 (`RUNTIME_MIGRATIONS`). The controller runs independent loops (lease renewal, experiment view, signal intake, slots, reconciliation, outbox, heartbeat); model work runs in its own tasks, so a slow model call never blocks receipts, reconciliation or recovery. Ids, expiry, persistence and submission are code-owned; the engine only returns proposals. The compose `ai` service holds two RPC key pairs, the trader public key, `ai.yaml`, the `mmr_ai_data` volume and model-provider env only.

**Tech Stack:** Python 3.12, DuckDB through Plan 4's `AiStore`, pydantic v2 (config section), `asyncio`, SP1 typed RPC (`trader/messaging/typed_rpc.py`), `exchange_calendars` through `trader/automation/calendar_policy.py`, pytest, pytest-asyncio, PyYAML `safe_load`. No new dependencies.

**Spec:** `docs/superpowers/specs/2026-10-07-ai-paper-sp2ab-autonomous-loop-design.md`. Binding sections: 4 (components), 5.1 (leadership), 5.2 (scheduling), 5.3 (identity and submission), 5.5 (intake part: opportunity and cursor atomic, MISSED, coverage gap), 7 (outbox), 9 (failure handling), 12 ("Crash and leadership" 1–4, "Durability", "Signed epoch"). Index: `docs/superpowers/plans/2026-10-07-ai-paper-sp2ab-00-index.md` (shared names, `ai.duckdb` migrations 10–19, test commands). Depends on Plans 1, 2, 4 (names used exactly as their "Cross-plan additions" give them) and on Plan 3's `discover_ai_candidates` method name. Code base cited by file and function name (base: master after SP1 Plans 3–6).

## Global Constraints

- **Package:** new modules `trader/ai/ids.py`, `runtime_schema.py`, `rpc_clients.py`, `leadership.py`, `schedule.py`, `engine.py`, `submitter.py`, `outbox.py`, `signal_intake.py`, `controller.py`, and `trader/ai_service.py`. Tests in `tests/ai/runtime/`. Plan 4 files change only in `trader/ai/config.py` (one new section) and `config_defaults/ai.yaml` (its defaults).
- **Imports:** Plan 5 modules may import `trader.messaging.typed_rpc`, `trader.messaging.principals`, `trader.messaging.rpc_keys` and `trader.automation.calendar_policy`. They never import `ib_async`, `trader.trading`, `trader.data_providers`, `trader.scoreboard`, `trader.strategy`, `trader.trader_service` or `alpaca` (`tests/ai/runtime/test_runtime_isolation.py`). Plan 4's ten modules keep their stricter rule.
- **Migrations:** `ai.duckdb` versions **10–17** (`RUNTIME_MIGRATIONS`); 18–19 stay free. One plain `CREATE` per table, no `ALTER`, no backfill (no legacy data).
- **Blocking work:** every `TypedRpcClient.call` and every DuckDB access runs off the event loop (`asyncio.to_thread`, `AiStore.atransaction` / `aquery`).
- **Leadership:** lease 60 s, renew every 20 s, `holder_id = "ai-" + 12 hex` fresh per process. Local deadline = monotonic time taken before the grant request + lease − 5 s.
- **Ids:** decision `dec-` + 32 hex; command `aip-<decision_id>`; cost record `cost-` + 40 hex; attempt `att-` + 40 hex; simulated record `sim-` + 40 hex; cycle `cyc-<entry|position>-YYYYMMDD-HHMM` (New York time).
- **Decision expiry:** `decision_ttl_seconds` default 300, at most 900 (the trader's `MAX_EXPIRY_AHEAD` is 15 min). A send needs at least 5 s left (`SEND_MARGIN`).
- **No policy, no cap from an AI path:** `publish_ai_risk_policy` is in no client set. The budget cap is the owner's `ai_paper.model_budget_usd_per_day` in `trader.yaml` (owner, 2026-10-07): the controller reads it with the read-only query `get_ai_model_budget` (Plan 2) and applies it with Plan 4's `Budget.set_cap`. `ai.yaml` has no cap (Plan 4 Ruling 1); no client method writes one. A failed or malformed cap read stops new model calls until a read succeeds (Ruling 19).
- **Credentials:** the `ai` container gets model-provider env only. No `ALPACA_*`, `IB_*`, `TWS_*`, `MASSIVE_*`, `TWELVEDATA_*` and no `trader.yaml`. Never log a key, a token or a request body field that came from env.
- **Strict input:** trader replies are checked with `type(x) is int` / ISO-8601-with-offset parsing before use; a malformed reply fails loudly and commits nothing.
- **DuckDB:** only through `AiStore.transaction` / `atransaction` / `aquery` (Plan 4). No long-lived connection.
- **YAML:** `yaml.safe_load` only.
- **Fail loudly:** every refusal and every dead letter has its own code and an ERROR log; nothing returns an empty result on error. A trader that cannot be reached is "unknown", never "no experiment" and never "no signals".
- **Tests:** no model call, no IB call, no network beyond loopback. Every async test carries `@pytest.mark.asyncio`; async fixtures use `pytest_asyncio.fixture`. Per task: `.venv/bin/python -m pytest <files> -q --timeout=60`. Full suite once, in Task 12: `.venv/bin/python -m pytest tests/ -q --timeout=60 --ignore=tests/test_ibrx_async.py`.
- Commit subjects `feat: ...` / `test: ...` / `docs: ...`, lowercase, imperative. Every commit message ends with a blank line and `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.
- No container restart, broker order, deploy or push is authorized by this plan.

## Rulings

1. **Sockets.** `AiRpcClients` holds two `PrincipalClient`s. `supervisor` has three sockets: command, query and a **separate discovery query socket** used only by `discover_ai_candidates` with a 90 s timeout (Plan 3). `TypedRpcClient` holds a lock for a whole call, so a 90 s discovery read on the shared query socket would block `read_ai_signals` and `get_ai_paper_decision`; a own socket keeps spec 5.2's "slow work never blocks receipts or reconciliation". `research` has command and query. *Cost if wrong:* one extra ZMQ socket.
2. **Method sets and the epoch.** `SUPERVISOR_COMMANDS = {grant_ai_controller_epoch, submit_ai_paper_decision, record_ai_cost, record_simulated_decision}`, `SUPERVISOR_QUERIES = {read_ai_signals, get_ai_paper_decision, get_experiment, get_experiment_trips, get_ai_risk_policy, get_ai_deployment, get_snapshot, get_positions, get_ai_model_budget}`, `SUPERVISOR_SLOW_QUERIES = {discover_ai_candidates: 90.0}`, `RESEARCH_COMMANDS = {register_ai_deployment}`, `RESEARCH_QUERIES = {get_ai_deployment}`. A method outside the set raises `MethodNotAllowedLocally` before signing. `EPOCH_METHODS = {submit_ai_paper_decision, read_ai_signals, get_ai_paper_decision}` carry the held epoch; with no held epoch they fail locally (`NOT_LEADER`). Other methods carry no epoch (Plan 2: ingestion needs none; the grant reads its body). *Cost if wrong:* Plan 6 adds a query by one line in the set (the ACL test pins it).
3. **What the trader can have seen.** `ConnectionError` from `TypedRpcClient.call` happens only before the send (no socket, or `IMMEDIATE` refused the queue) → `RpcNotSent` (proven not sent). `TimeoutError`, a reply that fails verification, or any other error after the send → `RpcOutcomeUnknown`. `TypedRpcRemoteError` → `RpcRefused` (the trader answered).
4. **Leadership and restart.** One fresh `holder_id` per process. A restarted process is a new holder and waits up to one lease for the old one to expire (Plan 1 Ruling 3; up to 60 s). The gateway's restart recovery (`ModelGateway.start`, Plan 4 Ruling 11) runs only after the epoch is held. The held epoch is written to `ai_held_epochs` before it is used.
5. **Losing leadership.** Leadership ends at once on `CONTROLLER_EPOCH_HELD` / `CONTROLLER_EPOCH_UNKNOWN` from a renewal, on `CONTROLLER_EPOCH_STALE` / `_MISSING` from any epoch method (also a `REJECTED` receipt with that code), and when the local deadline passes without a renewal. After a loss the process keeps asking with its last epoch: the trader renews it while its lease lives, otherwise grants the next epoch (Plan 1 Ruling 3). After `CONTROLLER_EPOCH_UNKNOWN` it asks with `null`.
6. **Slot alignment.** Slots start at `open + k × interval` (k ≥ 0). An entry slot counts only if `opening_stabilization_end ≤ start < entry_cutoff` (09:45 … 15:15 on a normal day with SP1's 5-minute stabilization); its work must end by `min(start + interval, entry_cutoff)`. *Cost if wrong:* slots shift by up to one interval.
7. **Position cycles.** Same grid and interval (configurable), counted while `opening_stabilization_end ≤ start < flatten_start` (`SessionSchedule.flatten_start_utc`, 15:45 ET on a normal day; with 15-minute slots the last position slot starts at 15:30, one slot after the last entry slot at 15:15, and ends at 15:45); work ends by `min(start + interval, flatten_start)`. They run while the experiment is `ARMED` or `PAUSED` and `get_experiment_trips` shows an `OPEN` trip with quantity left. `KILLED`: no cycles and no entry signals; exit signals still produce closes and unsent, unexpired closes are still sent (the trader joins them to the kill flatten); unsent `ENTER`s wait and expire. `STOPPED`: nothing new; reconciliation and reporting continue.
8. **Missed slots and bounded work.** A slot may start only within `slot_start_grace_seconds` (default 120) of its start. A later first sight records it `MISSED` (`LATE_START`); it is never run. A slot of a kind whose previous cycle still runs is `MISSED` (`PREVIOUS_CYCLE_RUNNING`). A cycle is cancelled at its slot deadline (`TIMED_OUT`); nothing from it is persisted. After a restart `RUNNING` cycles become `FAILED` (`PROCESS_RESTARTED`), never replayed.
9. **Signals.** An opportunity is stale when `now − signal_time > signal_max_age_seconds` (default 300) → `MISSED` (`STALE`). `BUY` needs `ARMED`, no entry block and an open entry window, else `MISSED` with the reason. `SELL` is judged in `ARMED`, `PAUSED` and `KILLED`, `MISSED` in `STOPPED`. An unknown experiment (trader away) waits; it is never "no experiment". An opportunity left `IN_PROGRESS` by a crash is judged again if still fresh, else `MISSED`: nothing was persisted for it, because decisions and the opportunity state commit in one transaction. `SIGNAL_CURSOR_AHEAD` (the trader's record was reset) writes a `CURSOR_AHEAD` coverage gap and restarts from cursor 0; redelivered signals are deduplicated by `source_event_id`.
10. **Submission state machine.** `PENDING` (persisted, not sent) → `SENDING` (written before any byte leaves) → `ACCEPTED` (trader receipt, non-final) / `FINAL` (receipt `RESOLVED` or `REJECTED`) / `UNKNOWN` (possible send) / back to `PENDING` (proven not sent) / `FAILED` (`VALIDATION_ERROR`, `PERMISSION_DENIED`, `METHOD_NOT_ALLOWED`: a bug). Epoch refusals and `AUTHENTICATION_ERROR` / `REPLAY_ERROR` are "refused before any handler" (Plan 1 Ruling 1a: no ledger row) → `PENDING` for a first send, `UNKNOWN` for a resend (an earlier send may have landed). Any other trader error code → `UNKNOWN`. Unsent work is `ABANDONED` on expiry (`EXPIRED_UNSENT`), on a closed entry window for an `ENTER` (`OUTSIDE_ENTRY_WINDOW`) or on a `STOPPED` experiment for a close. `UNKNOWN` is reconciled with `get_ai_paper_decision` under the current epoch: found → receipt saved; not found and `not_found_settle_seconds` (default 120, above the 30 s RPC clock skew) past the last send → resend the same id and the same stored body bytes while unexpired, else `NOT_ADMITTED`. A `REJECTED` receipt with `CONTROLLER_EPOCH_STALE` is final: the decision is lost, never regenerated (Plan 1 Ruling 4). On restart `SENDING` → `UNKNOWN` (`PROCESS_RESTARTED`).
11. **Ids and expiry are code-owned.** `decision_id = "dec-" + sha256(source_id + "|" + action_key)[:32]`; `source_id` is the signal's `source_event_id` or the cycle id; `action_key` (`^[a-z][a-z_]{0,15}:[0-9]{1,12}(:[a-z0-9_]{1,24})?$`, e.g. `enter:265598`) comes from the engine. `expires_at = decided_at + decision_ttl_seconds`. The engine never sets either.
12. **Cost mapping (coordinator ruling, binding).** Plan 2's ids allow only `[A-Za-z0-9_-]{8,96}`, but Plan 4's `event_id` (`<attempt_key>:<kind>`) and `attempt_key` (`<decision_id>/<role>/<seq>#<n>`) contain `/`, `#`, `:`. So `record_id = "cost-" + sha256(event_id).hexdigest()[:40]` and `attempt_id = "att-" + sha256(attempt_key).hexdigest()[:40]`; the outbox row keeps the original `event_id` (`source_ref`) and `attempt_key` next to it for traceability. Status: `CONFIRMED` → `confirmed` with the actual cost; `ESTIMATED_UNKNOWN` → `estimated` with the reserved worst-case cost (never `confirmed`); `NONE` → `confirmed`, `0.0` (a proven not-sent or rejected call costs nothing, and spec 7 wants failed attempts reported); `CORRECTION` → `confirmed`, a new record whose `corrects_record_id` is the mapped id of the attempt's `ESTIMATED_UNKNOWN` record. Money: `cost_usd = cost_micros / 1_000_000` through `Decimal` (Plan 4 money is integer micro-USD; pinned by a test). A `REFUSED` reply (for example `CORRECTION_TARGET_UNKNOWN`, `CONFLICTING_DUPLICATE`) follows Plan 2's `retryable` flag (Ruling 14). `called_at` = the attempt's `started_at` (same for original and correction, as Plan 2 Ruling 4 requires). `experiment_id`, `served_kind`, `served_id` come from `ai_call_contexts`, keyed by the part of `request_key` before the first `/`. `decision_id` is always `null`: a Jev `SKIP` never creates a trader decision and a link to an unknown decision would retry forever (Plan 2 Ruling 5); `served_kind = "decision"` with `served_id = decision_id` carries the link. *Cost if wrong:* the report joins costs to decisions by `served_id`.
13. **Baselines.** `record_id = simulated_record_id(experiment_id, baseline_id, opportunity_id)`. A baseline linked to a decision of the same result waits (`WAITING`) until that submission is settled; then `linked_decision_id` is the decision id if the trader has a row (`ACCEPTED`, `FINAL`), else `null` (`ABANDONED`, `NOT_ADMITTED`, `FAILED`). The body changes only before the first delivery.
14. **Outbox order and dead letters.** Delivery follows `created_seq`. The first transport failure ends the pass (trader away; keep order). `INSERTED` / `DUPLICATE` → `DELIVERED`. `REFUSED` with `retryable: true` and other trader error codes → retry with backoff `min(5 × 2^attempts, 300)` s. `REFUSED` with `retryable: false`, `VALIDATION_ERROR`, `PERMISSION_DENIED`, `METHOD_NOT_ALLOWED` → `DEAD` (ERROR log, counted in the heartbeat). A cost event without its context → `DEAD` (`CONTEXT_MISSING`).
15. **Engine contract.** Hooks return an `EngineResult`; they must not raise for model failures (Plan 6 turns those into a result with baselines and a note). A raise is a bug → the opportunity or cycle is `FAILED` (`ENGINE_ERROR`). A result whose action does not fit its source (an `ENTER` from a position cycle or exit signal, a close from an entry signal or cycle), a repeated `action_key` or a baseline link to an unknown `action_key` is refused whole (`FAILED`, nothing persisted).
16. **Engine seam.** `trader.ai_service.build_engine(deps)` raises `EngineNotInstalled` until Plan 6 replaces its body; `main()` then exits with code 2 and an ERROR line. Tests inject engines through `run_service(..., engine_factory=...)`.
17. **Compose (owner to confirm the profile).** The `ai` service is in profile `ai` (opt-in: `docker compose --profile ai up -d ai`), because the shipped `ai.yaml` has blank model ids and the service would restart-loop. It does not inherit `x-mmr-common-env` (Alpaca keys) and does not mount `~/.config/mmr` (its `trader.yaml` may hold `alpaca_api_key_id`): it binds only `ai.yaml` read-only, its two key pairs and `trader.pub`. Data: named volume `mmr_ai_data` at `/home/trader/.local/share/mmr_ai` (the parent of Plan 4's default `database_path`). Healthcheck: the heartbeat file in `/tmp` is younger than 120 s; no port is bound or published. The trader address comes from env `TRADER_TYPED_ADDRESS` (the name the other services use), the ports from `ai.yaml`.
18. **Two keys in one service.** `principals.SERVICE_PRINCIPAL["ai"] = "ai_supervisor"` plus `SERVICE_EXTRA_PRINCIPALS = {"ai": ("ai_research",)}`; `service_principals(service)` and `service_rpc_files(service)` replace direct `rpc_files_for(SERVICE_PRINCIPAL[...])` uses in the key-mount check and its tests. `RESTART_ON_ROTATE` counts every principal a service signs as (`ai_research` → `("ai", "trader")`).
19. **Owner budget cap (owner, 2026-10-07; spec 5.4).** `BudgetCapSync` reads `get_ai_model_budget` at `start()` and then every `budget_cap_poll_seconds` (default 60), and calls `Budget.set_cap(usd_to_micros_floor(value))` after each good read. The reply must be exactly `{"model_budget_usd_per_day": <int or float, not bool, finite, ≥ 0>, "source": "trader.yaml"}`; anything else is a failed read. Plan 4's `set_cap` keeps the persisted rules: lowering applies at once, a raise waits for 00:00 America/New_York, and an `ai` restart that reads the same raise gets `RAISE_KEPT` (never early). The periodic read means a raise the operator made before midnight is scheduled for that midnight. **Fail closed:** the cap is *ready* only when the latest read succeeded **and** it was made in the current New York window; otherwise the gateway the engine and the controller use (`CapGatedGateway`) refuses every model call with `CallRefused("BUDGET_CAP_UNKNOWN")` before any reservation. Only model calls stop: signal intake, evidence reads, baselines, reconciliation, the outbox and exit-signal closes keep running (spec 5.4: exhaustion blocks new model work only). *Cost if wrong:* up to one poll interval (60 s) without model calls after New York midnight and after every trader outage.

## Cross-plan additions

Plan 6 (and any later plan) uses these exact names.

- `trader.ai.ids`: `ACTION_KEY` (regex above), `derive_decision_id(source_id: str, action_key: str) -> str`, `command_id_for(decision_id) -> str`, `canonical_json(value) -> str`, `cost_record_id(event_id) = "cost-" + sha256(event_id).hexdigest()[:40]`, `attempt_ref(attempt_key) = "att-" + sha256(attempt_key).hexdigest()[:40]`, `simulated_record_id(experiment_id, baseline_id, opportunity_id) = "sim-" + sha256(f"{experiment_id}|{baseline_id}|{opportunity_id}").hexdigest()[:40]`.
- Plan 4 cost event → Plan 2 `record_ai_cost` (`trader.ai.outbox.cost_body(event, attempt, context)`): `CONFIRMED` → `confirmed` + actual cost; `ESTIMATED_UNKNOWN` → `estimated` + reserved worst case; `NONE` → `confirmed` + `0.0`; `CORRECTION` → `confirmed` + `corrects_record_id = cost_record_id(f"{attempt_key}:ESTIMATED_UNKNOWN")`; `cost_usd = float(Decimal(cost_micros) / 1_000_000)`; `decision_id` always null; `served_kind` / `served_id` / `experiment_id` from `ai_call_contexts`.
- `trader.ai.engine`:
  - `ExperimentView(experiment_id: str, state: str, started_at: datetime, entry_block: Optional[str])`, `ExperimentView.from_reply(get_experiment reply) -> Optional[ExperimentView]`.
  - `SignalOpportunity(opportunity_id, signal_cursor: int, strategy_name, conid: int, action: "BUY"|"SELL", probability: Optional[float], signal_time: datetime, recorded_at: datetime)`; `opportunity_id` is the trader's `source_event_id`.
  - `OwnedPosition(round_trip_id, conid: int, symbol, open_quantity: float, opened_at: datetime, decision_id: Optional[str])`; `owned_positions_from_trips(reply) -> tuple[OwnedPosition, ...]`.
  - `ModelWork` with `.context_key`, `.served_kind`, `.served_id`, `.source_id`, `.experiment_id`, `.gateway: ModelCaller`, `.deadline: DecisionDeadline`, `request_key(role: str, call_seq: int) -> str` (`"<context_key>/<role>/<call_seq>"`, Plan 4 Ruling 10) and `async for_action(action_key) -> ModelWork` (context = the derived decision id, `served_kind = "decision"`, same deadline).
  - `SignalContext(now, experiment, opportunity, work)`, `EntryCycleContext(now, experiment, slot, work)`, `PositionCycleContext(now, experiment, slot, positions, work)`.
  - `ProposedDecision(action_key, action, conid, side, decider, evidence_digest, deployment_digest=None, policy_revision=None, stop_price=None, target_price=None, quantity=None)`.
  - `SimulatedBaseline(baseline_id, cohort, opportunity_id, decided_at, conid=None, side=None, quantity=None, reference_price=None, stop_price=None, target_price=None, linked_action_key=None, linked_decision_id=None, linked_round_trip_id=None, deployment_digest=None, incomplete_reason=None)`; it enforces Plan 2's shapes (`follow_signal.v1` / `fixed_rule.v1`: `quantity` null and `deployment_digest` set; matched-entry: `quantity` set; with `incomplete_reason`: no side, quantity or prices). `BASELINE_COHORTS` (the index pairs), `TRADER_SIZED_BASELINES = {"follow_signal.v1", "fixed_rule.v1"}`, `INCOMPLETE_REASONS = ("quote_unavailable", "feed_not_accepted", "budget_refused", "model_failed", "sizing_unavailable")`.
  - `EngineResult(decisions=(), baselines=(), note="")`.
  - `class DecisionEngine(Protocol)`: `async on_entry_signal(SignalContext)`, `async on_exit_signal(SignalContext)`, `async on_entry_cycle(EntryCycleContext)`, `async on_position_cycle(PositionCycleContext)`, each `-> EngineResult`.
- `trader.ai.rpc_clients`: `ReadOnlySupervisor(supervisor).call(method, body)` allows `ENGINE_QUERIES = SUPERVISOR_QUERIES ∪ SUPERVISOR_SLOW_QUERIES − EPOCH_METHODS` only; errors `RpcNotSent`, `MethodNotAllowedLocally`, `RpcOutcomeUnknown`, `RpcRefused`.
- `trader.ai_service`: `EngineDeps(config: AiConfig, gateway: ModelCaller, reads: ReadOnlySupervisor, clock: Clock, recorder: ReplayRecorder)`; `build_engine(deps) -> DecisionEngine` (Plan 6 replaces the body); `run_service(settings, *, engine_factory, clock=None, environ=None, wrap_clients=None) -> int`; `serve(settings, *, engine_factory, stop, clock=None, environ=None, wrap_clients=None)`; `ServiceSettings(config_path, keys_dir, trader_address)`.
- `trader.ai.config.ControllerConfig` (section `controller:` of `ai.yaml`) and `AiConfig.controller`.
- `trader.ai.runtime_schema`: `RUNTIME_MIGRATIONS` (10–17), `ALL_MIGRATIONS`; tables `ai_held_epochs`, `ai_submissions`, `ai_outbox`, `ai_cursors`, `ai_opportunities`, `ai_coverage_gaps`, `ai_call_contexts`, `ai_cycles`. Plan 6 starts at 20.
- `trader.messaging.principals`: `SERVICE_EXTRA_PRINCIPALS`, `service_principals(service)`, `service_rpc_files(service)`.
- Test helpers Plan 6 may reuse: `tests/ai/runtime/fakes.py`, `tests/ai/runtime/scripted_engine.py` (`ScriptedEngine`), `tests/ai/runtime/trader_world.py` (`TraderWorld`, `AiNode`, `TraderClock`, `FlakyClient`, `write_service_config`).

## Review Focus

1. **The trader accepted a decision and the process died before saving the receipt.** The restarted process (a real new OS process) waits for the old lease, takes epoch 2, reconciles the same id without a resend, recovers the receipt, and the position stays protected with one entry. → Task 12 `test_trader_accepted_receipt_not_saved_survives_a_real_process_restart`.
2. **A submit reply is lost and another controller takes over.** The successor reconciles by `get_ai_paper_decision` under its own epoch; no second entry. → Task 11 `test_takeover_around_a_lost_submit_reply`; Task 6 `test_a_lost_reply_is_reconciled_by_the_same_id_without_a_resend`.
3. **A paused controller wakes after a takeover and sends.** The trader refuses its epoch, no ledger row is written, the command stays pending and the successor sends the same id and body. → Task 11 `test_stale_controller_is_refused_by_its_epoch`; Task 6 `test_an_epoch_refusal_keeps_the_command_pending_and_drops_leadership`.
4. **A crash between signal intake and cursor advance.** No signal is skipped and none is judged twice. → Task 8 `test_crash_between_intake_and_cursor_skips_no_signal`, `test_a_redelivered_signal_is_not_a_new_opportunity`.
5. **The trader is down, then an acknowledgement is lost.** Costs and baselines arrive once. → Task 7 `test_delivery_survives_a_trader_outage`, `test_a_lost_acknowledgement_never_duplicates`; Task 11 `test_outbox_delivers_after_a_trader_outage_without_duplicates`.
6. **The owner's cap.** It comes only from the trader (`trader.yaml`); an `ai` restart or an `ai.yaml` edit cannot raise it early; a failed or malformed read stops model calls (not reconciliation, the outbox or closes) until a read succeeds. → Task 9 `test_the_cap_comes_from_the_trader_and_a_raise_waits_for_new_york_midnight`, `test_an_ai_restart_or_ai_config_change_cannot_raise_the_cap_early`, `test_a_failed_cap_read_stops_model_calls_until_a_read_succeeds`, `test_a_closed_cap_gate_keeps_reconciliation_and_the_outbox_running`; Task 11 `test_the_owner_cap_is_read_from_trader_yaml_over_signed_rpc`.

---

## File map

| File | Responsibility | Task |
|---|---|---|
| `trader/ai/config.py`, `config_defaults/ai.yaml`, `trader/ai/runtime_schema.py` | controller config section, migrations 10–17, cursor helpers | 1 |
| `trader/ai/rpc_clients.py`, `tests/ai/runtime/fakes.py` | method-restricted clients, error classes | 2 |
| `trader/ai/leadership.py` | epoch grant, renew, loss | 3 |
| `trader/ai/schedule.py` | XNYS slots | 4 |
| `trader/ai/ids.py`, `trader/ai/engine.py`, `tests/ai/runtime/scripted_engine.py` | engine contract, ids, fake engine | 5 |
| `trader/ai/submitter.py` | persist-before-send, reconcile | 6 |
| `trader/ai/outbox.py` | costs and baselines to the trader | 7 |
| `trader/ai/signal_intake.py` | signal cursor, opportunities, gaps | 8 |
| `trader/ai/controller.py` | loops, dispatch, cycles, commit | 9 |
| `trader/ai_service.py`, `docker-compose.yml`, `docker.sh`, `trader/messaging/principals.py`, `trader/messaging/keys_cli.py`, `trader/messaging/rpc_keys.py` | entry point, container, keys | 10 |
| `tests/ai/runtime/trader_world.py`, `tests/ai/runtime/test_controller_integration.py` | SP1 coordinator integration | 11 |
| `tests/ai/runtime/child_service.py`, `tests/ai/runtime/test_crash_restart_subprocess.py`, `AGENTS.md`, `docs/OPERATIONAL_STATE.md` | real process restart, docs, full suite | 12 |

---

### Task 1: Controller config section and `ai.duckdb` migrations 10–17

**Files:**
- Modify: `trader/ai/config.py` (`ControllerConfig`, `_RawConfig.controller`, `AiConfig.controller`, `_check`, `AiConfig.digest`)
- Modify: `config_defaults/ai.yaml` (a `controller:` block with every default)
- Create: `trader/ai/runtime_schema.py`
- Create (tests): `tests/ai/runtime/__init__.py` (empty), `tests/ai/runtime/test_runtime_config.py`, `tests/ai/runtime/test_runtime_schema.py`

**Interfaces:**
- Consumes: Plan 4 `_Section`, `Whole`, `Number`, `AiConfigError`, `load_ai_config`, `Migration`, `FOUNDATION_MIGRATIONS`, `AiStore`, `to_utc`; test helpers `tests.ai.fakes.config_text`, `write_config`, `FakeClock`.
- Produces: `ControllerConfig`; `AiConfig.controller: ControllerConfig`; `RUNTIME_MIGRATIONS`, `ALL_MIGRATIONS`; `cursor_value_in_tx(conn, name) -> int`, `set_cursor_in_tx(conn, name, value, now) -> None`, `async read_cursor(store, name) -> int`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/ai/runtime/test_runtime_config.py
"""SP2 Plan 5 Task 1: the controller section of ai.yaml."""
from pathlib import Path

import pytest
import yaml

from tests.ai.fakes import config_text, write_config
from trader.ai.config import AiConfigError, ControllerConfig, load_ai_config

TEMPLATE = Path(__file__).resolve().parents[3] / "config_defaults" / "ai.yaml"


def load(directory: Path, block: str = ""):
    directory.mkdir(parents=True, exist_ok=True)
    return load_ai_config(str(write_config(directory, config_text(extra_top_level=block))))


def test_defaults_follow_the_index(tmp_path):
    c = load(tmp_path).controller
    assert (c.lease_seconds, c.renew_seconds, c.entry_slot_minutes, c.position_slot_minutes) == (60, 20, 15, 15)
    assert (c.trader_query_port, c.trader_command_port, c.decision_ttl_seconds) == (42101, 42102, 300)
    assert (c.slot_start_grace_seconds, c.signal_max_age_seconds, c.not_found_settle_seconds) == (120, 300, 120)


def test_values_are_read_and_change_the_digest(tmp_path):
    plain = load(tmp_path / "a")
    custom = load(tmp_path / "b", "controller: {entry_slot_minutes: 30, signal_poll_seconds: 0.5}")
    assert (custom.controller.entry_slot_minutes, custom.controller.signal_poll_seconds) == (30, 0.5)
    assert plain.digest() != custom.digest()


@pytest.mark.parametrize("block,code", [
    ("controller: {renew_seconds: 30}", "CONTROLLER_RENEW_TOO_SLOW"),
    ("controller: {entry_slot_minutes: 5, slot_start_grace_seconds: 300}", "CONTROLLER_GRACE_TOO_LONG"),
    ("controller: {decision_ttl_seconds: 901}", "AI_CONFIG_INVALID"),
    ("controller: {lease_seconds: true}", "AI_CONFIG_INVALID"),
    ("controller: {not_found_settle_seconds: 30}", "AI_CONFIG_INVALID"),
    ("controller: {unknown_key: 1}", "AI_CONFIG_INVALID"),
])
def test_bad_controller_config_fails_loudly(tmp_path, block, code):
    with pytest.raises(AiConfigError) as exc:
        load(tmp_path, block)
    assert exc.value.code == code


def test_shipped_template_lists_the_controller_defaults():
    shipped = yaml.safe_load(TEMPLATE.read_text())["controller"]
    assert shipped == ControllerConfig().model_dump()
```

```python
# tests/ai/runtime/test_runtime_schema.py
"""SP2 Plan 5 Task 1: ai.duckdb migrations 10-17 and the cursor helpers."""
import datetime as dt

import pytest

from tests.ai.fakes import FakeClock
from trader.ai.runtime_schema import ALL_MIGRATIONS, RUNTIME_MIGRATIONS, read_cursor, set_cursor_in_tx
from trader.ai.store import AiStore

TABLES = {"ai_held_epochs", "ai_submissions", "ai_outbox", "ai_cursors", "ai_opportunities",
          "ai_coverage_gaps", "ai_call_contexts", "ai_cycles"}


def test_plan_5_owns_10_to_17():
    assert [m.version for m in RUNTIME_MIGRATIONS] == list(range(10, 18))


def test_migrations_apply_once_and_survive_reopen(tmp_path):
    clock = FakeClock(dt.datetime(2026, 7, 17, 15, 0, tzinfo=dt.timezone.utc))
    store = AiStore(tmp_path / "ai.duckdb", clock=clock)
    assert store.migrate(ALL_MIGRATIONS)[-8:] == list(range(10, 18))
    assert AiStore(tmp_path / "ai.duckdb", clock=clock).migrate(ALL_MIGRATIONS) == []
    names = {row[0] for row in store.db.execute(
        "SELECT table_name FROM information_schema.tables", fetch="all")}
    assert TABLES <= names


@pytest.mark.asyncio
async def test_cursor_starts_at_zero_and_moves_only_in_a_transaction(tmp_path):
    clock = FakeClock(dt.datetime(2026, 7, 17, 15, 0, tzinfo=dt.timezone.utc))
    store = AiStore(tmp_path / "ai.duckdb", clock=clock)
    store.migrate(ALL_MIGRATIONS)
    assert await read_cursor(store, "signals") == 0
    await store.atransaction(lambda conn: set_cursor_in_tx(conn, "signals", 7, clock.now()))
    assert await read_cursor(store, "signals") == 7
    with pytest.raises(ValueError):
        await store.atransaction(lambda conn: set_cursor_in_tx(conn, "signals", True, clock.now()))
```

- [ ] **Step 2: Run them and see them fail**

Run: `.venv/bin/python -m pytest tests/ai/runtime/test_runtime_config.py tests/ai/runtime/test_runtime_schema.py -q --timeout=60`
Expected: `ImportError: cannot import name 'ControllerConfig'` and `ModuleNotFoundError: No module named 'trader.ai.runtime_schema'`.

- [ ] **Step 3: Implement**

In `trader/ai/config.py`, after `BudgetConfig`:

```python
class ControllerConfig(_Section):
    """The controller runtime (SP2 Plan 5). Defaults follow the index: lease 60 s, renew 20 s, slots 15 min."""
    trader_query_port: Whole = Field(42101, gt=0, lt=65536)
    trader_command_port: Whole = Field(42102, gt=0, lt=65536)
    rpc_timeout_seconds: Number = Field(10.0, gt=0, le=60, allow_inf_nan=False)
    lease_seconds: Whole = Field(60, ge=10, le=600)
    renew_seconds: Whole = Field(20, gt=0, le=200)
    held_retry_seconds: Number = Field(5.0, gt=0, le=60, allow_inf_nan=False)
    entry_slot_minutes: Whole = Field(15, ge=5, le=60)
    position_slot_minutes: Whole = Field(15, ge=5, le=60)
    slot_start_grace_seconds: Whole = Field(120, ge=10, le=600)
    signal_poll_seconds: Number = Field(5.0, gt=0, le=60, allow_inf_nan=False)
    signal_page_limit: Whole = Field(100, ge=1, le=500)
    signal_max_age_seconds: Whole = Field(300, ge=30, le=3600)
    decision_ttl_seconds: Whole = Field(300, ge=30, le=900)
    not_found_settle_seconds: Whole = Field(120, ge=60, le=900)
    reconcile_seconds: Number = Field(10.0, gt=0, le=300, allow_inf_nan=False)
    outbox_seconds: Number = Field(5.0, gt=0, le=300, allow_inf_nan=False)
    experiment_poll_seconds: Number = Field(10.0, gt=0, le=300, allow_inf_nan=False)
    heartbeat_seconds: Number = Field(10.0, gt=0, le=60, allow_inf_nan=False)
    budget_cap_poll_seconds: Number = Field(60.0, gt=0, le=300, allow_inf_nan=False)
    heartbeat_path: StrictStr = "/tmp/mmr_ai_heartbeat.json"
```

`_RawConfig` gains `controller: ControllerConfig = Field(default_factory=ControllerConfig)`. `AiConfig` gains a last field `controller: ControllerConfig = ControllerConfig()` (a default, so Plan 4's positional constructor calls still work), and `digest()` adds `"controller": self.controller.model_dump()` to its body. At the end of `_check`, before the `return`:

```python
    controller = parsed.controller
    if controller.renew_seconds * 3 > controller.lease_seconds:
        raise AiConfigError("CONTROLLER_RENEW_TOO_SLOW", "renew_seconds must be at most a third of lease_seconds")
    shortest_slot = min(controller.entry_slot_minutes, controller.position_slot_minutes) * 60
    if controller.slot_start_grace_seconds >= shortest_slot:
        raise AiConfigError("CONTROLLER_GRACE_TOO_LONG", "slot_start_grace_seconds must be shorter than a slot")
```

and the return becomes `AiConfig(dict(parsed.roles), PriceBook(rows), parsed.budget, parsed.database_path, controller)`.

`config_defaults/ai.yaml`, appended:

```yaml
# The controller runtime (SP2 Plan 5). Every key is optional; these are the defaults.
# The trader host comes from the TRADER_TYPED_ADDRESS environment variable (tcp://trader in Docker).
controller:
  trader_query_port: 42101
  trader_command_port: 42102
  rpc_timeout_seconds: 10.0
  lease_seconds: 60
  renew_seconds: 20
  held_retry_seconds: 5.0
  entry_slot_minutes: 15
  position_slot_minutes: 15
  slot_start_grace_seconds: 120
  signal_poll_seconds: 5.0
  signal_page_limit: 100
  signal_max_age_seconds: 300
  decision_ttl_seconds: 300
  not_found_settle_seconds: 120
  reconcile_seconds: 10.0
  outbox_seconds: 5.0
  experiment_poll_seconds: 10.0
  heartbeat_seconds: 10.0
  budget_cap_poll_seconds: 60.0   # how often the owner's cap is read from the trader (trader.yaml)
  heartbeat_path: /tmp/mmr_ai_heartbeat.json
```

```python
# trader/ai/runtime_schema.py
"""ai.duckdb tables owned by SP2 Plan 5: migrations 10-17 (18-19 stay free). Plain CREATE only."""
from __future__ import annotations

from datetime import datetime
from typing import Any

from trader.ai.schema import FOUNDATION_MIGRATIONS, Migration
from trader.ai.store import AiStore

RUNTIME_MIGRATIONS: tuple[Migration, ...] = (
    Migration(10, "ai_held_epochs", ("""
        CREATE TABLE ai_held_epochs (
            epoch BIGINT PRIMARY KEY, holder_id VARCHAR NOT NULL,
            acquired_at TIMESTAMPTZ NOT NULL, renewed_at TIMESTAMPTZ NOT NULL,
            lease_expires_at TIMESTAMPTZ NOT NULL, lost_at TIMESTAMPTZ, lost_reason VARCHAR)""",)),
    Migration(11, "ai_submissions", ("""
        CREATE TABLE ai_submissions (
            decision_id VARCHAR PRIMARY KEY, command_id VARCHAR NOT NULL UNIQUE,
            source_kind VARCHAR NOT NULL CHECK (source_kind IN
                ('entry_signal', 'exit_signal', 'entry_cycle', 'position_cycle')),
            source_id VARCHAR NOT NULL, action_key VARCHAR NOT NULL,
            action VARCHAR NOT NULL CHECK (action IN ('ENTER', 'CLOSE', 'PARTIAL_CLOSE')),
            body_json VARCHAR NOT NULL, body_sha256 VARCHAR NOT NULL, expires_at TIMESTAMPTZ NOT NULL,
            created_epoch BIGINT NOT NULL,
            state VARCHAR NOT NULL CHECK (state IN ('PENDING', 'SENDING', 'UNKNOWN', 'ACCEPTED', 'FINAL',
                                                    'ABANDONED', 'NOT_ADMITTED', 'FAILED')),
            attempts INTEGER NOT NULL, last_epoch BIGINT, last_sent_at TIMESTAMPTZ,
            next_try_at TIMESTAMPTZ NOT NULL, receipt_json VARCHAR, receipt_state VARCHAR,
            close_root_id VARCHAR, error_code VARCHAR,
            created_at TIMESTAMPTZ NOT NULL, updated_at TIMESTAMPTZ NOT NULL,
            UNIQUE (source_id, action_key))""",)),
    Migration(12, "ai_outbox", ("CREATE SEQUENCE ai_outbox_seq START 1", """
        CREATE TABLE ai_outbox (
            record_id VARCHAR PRIMARY KEY,
            kind VARCHAR NOT NULL CHECK (kind IN ('cost', 'simulated')),
            source_ref VARCHAR NOT NULL, attempt_key VARCHAR, body_json VARCHAR NOT NULL,
            state VARCHAR NOT NULL CHECK (state IN ('WAITING', 'PENDING', 'DELIVERED', 'DEAD')),
            wait_for_decision_id VARCHAR, attempts INTEGER NOT NULL, next_try_at TIMESTAMPTZ NOT NULL,
            last_code VARCHAR, delivered_status VARCHAR,
            created_seq BIGINT NOT NULL DEFAULT nextval('ai_outbox_seq'),
            created_at TIMESTAMPTZ NOT NULL, delivered_at TIMESTAMPTZ)""")),
    Migration(13, "ai_cursors", ("""
        CREATE TABLE ai_cursors (
            name VARCHAR PRIMARY KEY, value BIGINT NOT NULL, updated_at TIMESTAMPTZ NOT NULL)""",)),
    Migration(14, "ai_opportunities", ("""
        CREATE TABLE ai_opportunities (
            opportunity_id VARCHAR PRIMARY KEY, signal_cursor BIGINT NOT NULL, strategy_name VARCHAR NOT NULL,
            conid BIGINT NOT NULL, action VARCHAR NOT NULL CHECK (action IN ('BUY', 'SELL')),
            probability DOUBLE, signal_time TIMESTAMPTZ NOT NULL, recorded_at TIMESTAMPTZ NOT NULL,
            state VARCHAR NOT NULL CHECK (state IN ('NEW', 'IN_PROGRESS', 'DECIDED', 'MISSED', 'FAILED')),
            reason VARCHAR, created_at TIMESTAMPTZ NOT NULL, updated_at TIMESTAMPTZ NOT NULL)""",)),
    Migration(15, "ai_coverage_gaps", ("""
        CREATE TABLE ai_coverage_gaps (
            gap_id VARCHAR PRIMARY KEY,
            kind VARCHAR NOT NULL CHECK (kind IN ('RETENTION', 'CURSOR_AHEAD')),
            after_cursor BIGINT NOT NULL, resumed_cursor BIGINT NOT NULL,
            detected_at TIMESTAMPTZ NOT NULL)""",)),
    Migration(16, "ai_call_contexts", ("""
        CREATE TABLE ai_call_contexts (
            context_key VARCHAR PRIMARY KEY, experiment_id VARCHAR NOT NULL,
            served_kind VARCHAR NOT NULL CHECK (served_kind IN ('decision', 'cycle', 'signal', 'research')),
            served_id VARCHAR NOT NULL, created_at TIMESTAMPTZ NOT NULL)""",)),
    Migration(17, "ai_cycles", ("""
        CREATE TABLE ai_cycles (
            cycle_id VARCHAR PRIMARY KEY, kind VARCHAR NOT NULL CHECK (kind IN ('entry', 'position')),
            session_date VARCHAR NOT NULL, slot_start TIMESTAMPTZ NOT NULL,
            state VARCHAR NOT NULL CHECK (state IN ('RUNNING', 'DONE', 'MISSED', 'SKIPPED', 'TIMED_OUT',
                                                    'FAILED')),
            reason VARCHAR, started_at TIMESTAMPTZ NOT NULL, finished_at TIMESTAMPTZ)""",)),
)

ALL_MIGRATIONS: tuple[Migration, ...] = FOUNDATION_MIGRATIONS + RUNTIME_MIGRATIONS


def cursor_value_in_tx(conn: Any, name: str) -> int:
    row = conn.execute("SELECT value FROM ai_cursors WHERE name = ?", [name]).fetchone()
    return 0 if row is None else int(row[0])


def set_cursor_in_tx(conn: Any, name: str, value: int, now: datetime) -> None:
    if type(value) is not int or value < 0:
        raise ValueError(f"cursor {name} must be a non-negative integer")
    conn.execute("INSERT INTO ai_cursors VALUES (?, ?, ?) ON CONFLICT (name) DO UPDATE "
                 "SET value = excluded.value, updated_at = excluded.updated_at", [name, value, now])


async def read_cursor(store: AiStore, name: str) -> int:
    return await store.atransaction(lambda conn: cursor_value_in_tx(conn, name))
```

- [ ] **Step 4: Run the tests and Plan 4's config tests**

Run: `.venv/bin/python -m pytest tests/ai/runtime/test_runtime_config.py tests/ai/runtime/test_runtime_schema.py tests/ai/test_config.py tests/ai/test_store.py -q --timeout=60`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add trader/ai/config.py config_defaults/ai.yaml trader/ai/runtime_schema.py tests/ai/runtime/__init__.py tests/ai/runtime/test_runtime_config.py tests/ai/runtime/test_runtime_schema.py
git commit -m "feat: add the ai controller config section and runtime migrations

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 2: Method-restricted `AiRpcClients`

**Files:**
- Create: `trader/ai/rpc_clients.py`
- Create (tests): `tests/ai/runtime/fakes.py`, `tests/ai/runtime/test_rpc_clients.py`

**Interfaces:**
- Consumes: Plan 1 `TypedRpcClient.call(method, body, response_model, timeout=None, *, on_behalf_of=None, controller_epoch=None)`, `ServiceIdentity.load(principal, keys_dir)`, `TypedRpcRemoteError`; `TRADER_ACL`; `tests.rpc_identity_fixtures.ServedStack`, `make_identities`.
- Produces: constants of Ruling 2; `RpcNotSent(code, detail="")`, `MethodNotAllowedLocally(RpcNotSent)`, `RpcOutcomeUnknown(code, detail="")`, `RpcRefused(code, message, details=None)`; `PrincipalClient` (`principal`, `methods`, `bind_epoch(source)`, `async call(method, body, *, epoch=None) -> dict`, `close()`); `AiRpcClients(supervisor, research)` with `from_sockets(...)`, `connect(...)`, `close()`; `ReadOnlySupervisor`, `ENGINE_QUERIES`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/ai/runtime/fakes.py
"""Shared fakes for the Plan 5 runtime tests. Later tasks append their own fakes here."""
from __future__ import annotations

import datetime as dt
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
UTC = dt.timezone.utc
FRIDAY = dt.date(2026, 7, 17)
AAPL, MSFT = 265598, 272093


def et(hour, minute, second=0, day=FRIDAY):
    return dt.datetime(day.year, day.month, day.day, hour, minute, second, tzinfo=ET).astimezone(UTC)


class FakeSocket:
    """Stands in for one TypedRpcClient: records calls, raises ``error`` or returns ``reply``."""

    def __init__(self, reply=None, error=None):
        self.calls, self.reply, self.error, self.closed = [], reply, error, False

    def call(self, method, body, response_model, timeout=None, **options):
        self.calls.append((method, body, timeout, options))
        if self.error is not None:
            raise self.error
        return {"method": method} if self.reply is None else self.reply

    def close(self):
        self.closed = True
```

```python
# tests/ai/runtime/test_rpc_clients.py
"""SP2 Plan 5 Task 2: two method-restricted clients over signed typed RPC (spec 4, 12 Security)."""
import pytest

from tests.ai.runtime.fakes import FakeSocket
from tests.rpc_identity_fixtures import ServedStack, make_identities
from trader.ai.rpc_clients import (
    EPOCH_METHODS, RESEARCH_COMMANDS, RESEARCH_QUERIES, SUPERVISOR_COMMANDS, SUPERVISOR_QUERIES,
    SUPERVISOR_SLOW_QUERIES, AiRpcClients, MethodNotAllowedLocally, ReadOnlySupervisor, RpcNotSent,
    RpcOutcomeUnknown, RpcRefused,
)
from trader.messaging.principals import TRADER_ACL
from trader.messaging.typed_rpc import AuthenticationError, TypedRpcRegistry, TypedRpcRemoteError


def fake_clients(epoch=None, error=None):
    sockets = {name: FakeSocket(error=error) for name in ("sc", "sq", "sd", "rc", "rq")}
    clients = AiRpcClients.from_sockets(
        supervisor_command=sockets["sc"], supervisor_query=sockets["sq"], supervisor_discovery=sockets["sd"],
        research_command=sockets["rc"], research_query=sockets["rq"], timeout=10.0)
    clients.supervisor.bind_epoch(lambda: epoch)
    return clients, sockets


@pytest.mark.asyncio
@pytest.mark.parametrize("who,method", [
    ("supervisor", "publish_ai_risk_policy"), ("supervisor", "register_ai_deployment"),
    ("supervisor", "pause_experiment"), ("supervisor", "start_experiment"),
    ("research", "submit_ai_paper_decision"), ("research", "grant_ai_controller_epoch"),
    ("research", "read_ai_signals"), ("research", "record_ai_cost")])
async def test_a_method_outside_the_set_fails_before_sending(who, method):
    clients, sockets = fake_clients(epoch=3)
    with pytest.raises(MethodNotAllowedLocally):
        await getattr(clients, who).call(method, {})
    assert all(socket.calls == [] for socket in sockets.values())


@pytest.mark.asyncio
@pytest.mark.parametrize("method", sorted(EPOCH_METHODS))
async def test_epoch_methods_need_a_held_epoch(method):
    clients, sockets = fake_clients(epoch=None)
    with pytest.raises(RpcNotSent) as exc:
        await clients.supervisor.call(method, {})
    assert exc.value.code == "NOT_LEADER" and sockets["sc"].calls == sockets["sq"].calls == []


@pytest.mark.asyncio
async def test_the_held_epoch_rides_only_on_epoch_methods():
    clients, sockets = fake_clients(epoch=4)
    await clients.supervisor.call("submit_ai_paper_decision", {"x": 1})
    await clients.supervisor.call("record_ai_cost", {"x": 2})
    await clients.supervisor.call("get_experiment", {})
    await clients.supervisor.call("get_ai_paper_decision", {"decision_id": "d"}, epoch=5)
    assert [c[3] for c in sockets["sc"].calls] == [{"controller_epoch": 4}, {}]
    assert [c[3] for c in sockets["sq"].calls] == [{}, {"controller_epoch": 5}]


@pytest.mark.asyncio
async def test_discovery_uses_its_own_socket_and_a_90_second_timeout():
    clients, sockets = fake_clients(epoch=1)
    await clients.supervisor.call("discover_ai_candidates", {})
    await clients.supervisor.call("get_snapshot", {})
    assert [(c[0], c[2]) for c in sockets["sd"].calls] == [("discover_ai_candidates", 90.0)]
    assert [(c[0], c[2]) for c in sockets["sq"].calls] == [("get_snapshot", 10.0)]


@pytest.mark.asyncio
@pytest.mark.parametrize("error,kind,code", [
    (ConnectionError("no route to server"), RpcNotSent, "TRADER_UNREACHABLE"),
    (TimeoutError("late"), RpcOutcomeUnknown, "REPLY_TIMEOUT"),
    (AuthenticationError("bad reply"), RpcOutcomeUnknown, "REPLY_UNTRUSTED"),
    (TypedRpcRemoteError("CONTROLLER_EPOCH_STALE", "stale"), RpcRefused, "CONTROLLER_EPOCH_STALE")])
async def test_errors_are_classified_by_what_the_trader_can_have_seen(error, kind, code):
    clients, _ = fake_clients(epoch=1, error=error)
    with pytest.raises(kind) as exc:
        await clients.supervisor.call("submit_ai_paper_decision", {})
    assert exc.value.code == code


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["submit_ai_paper_decision", "read_ai_signals", "record_ai_cost",
                                    "grant_ai_controller_epoch"])
async def test_read_only_supervisor_refuses_commands_and_epoch_methods(method):
    clients, sockets = fake_clients(epoch=1)
    with pytest.raises(MethodNotAllowedLocally):
        await ReadOnlySupervisor(clients.supervisor).call(method, {})
    assert await ReadOnlySupervisor(clients.supervisor).call("get_snapshot", {}) == {"method": "get_snapshot"}


def test_client_sets_match_the_trader_allow_list():
    for role, methods, principal in (
            ("command", SUPERVISOR_COMMANDS, "ai_supervisor"),
            ("query", SUPERVISOR_QUERIES | set(SUPERVISOR_SLOW_QUERIES), "ai_supervisor"),
            ("command", RESEARCH_COMMANDS, "ai_research"), ("query", RESEARCH_QUERIES, "ai_research")):
        for method in methods:
            assert principal in TRADER_ACL[(role, method)], (role, method)
    every = SUPERVISOR_COMMANDS | SUPERVISOR_QUERIES | set(SUPERVISOR_SLOW_QUERIES) | RESEARCH_COMMANDS
    assert "publish_ai_risk_policy" not in every                   # spec 6.7: SP2a/b never publishes policy


def test_cross_principal_calls_are_refused_through_signed_rpc():
    ids = make_identities()
    registry = TypedRpcRegistry(acl=TRADER_ACL)
    echo = lambda body, caller: {"principal": caller.principal, "epoch": caller.controller_epoch}  # noqa: E731
    for role, method in (("command", "submit_ai_paper_decision"), ("command", "register_ai_deployment"),
                         ("query", "get_experiment")):
        registry.register(role, method, dict, dict, echo, with_caller=True)
    served = ServedStack({("trader", "command"): registry, ("trader", "query"): registry}, ids)
    try:
        import asyncio
        clients = AiRpcClients.from_sockets(
            supervisor_command=served.client("ai_supervisor", role="command"),
            supervisor_query=served.client("ai_supervisor"), supervisor_discovery=served.client("ai_supervisor"),
            research_command=served.client("ai_research", role="command"),
            research_query=served.client("ai_research"), timeout=5.0)
        clients.supervisor.bind_epoch(lambda: 3)
        assert asyncio.run(clients.supervisor.call("submit_ai_paper_decision", {})) == \
            {"principal": "ai_supervisor", "epoch": 3}
        assert asyncio.run(clients.research.call("register_ai_deployment", {}))["principal"] == "ai_research"
        for principal, method in (("ai_research", "submit_ai_paper_decision"),
                                  ("ai_supervisor", "register_ai_deployment")):
            with pytest.raises(TypedRpcRemoteError) as exc:
                served.client(principal, role="command").call(method, {}, dict)
            assert exc.value.code == "PERMISSION_DENIED"
        with pytest.raises(TypedRpcRemoteError) as exc:                              # Plan 1 Ruling 6
            served.client("ai_research", role="command").call("register_ai_deployment", {}, dict,
                                                              controller_epoch=1)
        assert exc.value.code == "AUTHENTICATION_ERROR"
    finally:
        served.close()
```

- [ ] **Step 2: Run them and see them fail**

Run: `.venv/bin/python -m pytest tests/ai/runtime/test_rpc_clients.py -q --timeout=60`
Expected: `ModuleNotFoundError: No module named 'trader.ai.rpc_clients'`.

- [ ] **Step 3: Implement**

```python
# trader/ai/rpc_clients.py
"""Method-restricted typed RPC clients of the ai service (SP2 spec 4, Plan 5 Rulings 1-3).

Two principals in one process (spec 4 accepted limit: not process isolation).
Each client refuses a method outside its set before anything is signed or
sent. TypedRpcClient is blocking and holds a lock per call, so every call runs
on a worker thread, and the 90 s discovery read has its own socket.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Optional

SUPERVISOR_COMMANDS = frozenset({
    "grant_ai_controller_epoch", "submit_ai_paper_decision", "record_ai_cost", "record_simulated_decision"})
SUPERVISOR_QUERIES = frozenset({
    "read_ai_signals", "get_ai_paper_decision", "get_experiment", "get_experiment_trips", "get_ai_risk_policy",
    "get_ai_deployment", "get_snapshot", "get_positions", "get_ai_model_budget"})
SUPERVISOR_SLOW_QUERIES: Mapping[str, float] = {"discover_ai_candidates": 90.0}
RESEARCH_COMMANDS = frozenset({"register_ai_deployment"})
RESEARCH_QUERIES = frozenset({"get_ai_deployment"})
EPOCH_METHODS = frozenset({"submit_ai_paper_decision", "read_ai_signals", "get_ai_paper_decision"})
EPOCH_REFUSALS = frozenset({"CONTROLLER_EPOCH_MISSING", "CONTROLLER_EPOCH_STALE"})
ENGINE_QUERIES = (SUPERVISOR_QUERIES | frozenset(SUPERVISOR_SLOW_QUERIES)) - EPOCH_METHODS


class RpcNotSent(Exception):
    """Proven: the trader never received this request (local refusal, or no route before the send)."""

    def __init__(self, code: str, detail: str = ""):
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code, self.detail = code, detail


class MethodNotAllowedLocally(RpcNotSent):
    """The method is outside this principal's client set (Ruling 2)."""


class RpcOutcomeUnknown(Exception):
    """The request may have reached the trader; only a read can tell what happened."""

    def __init__(self, code: str, detail: str = ""):
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code, self.detail = code, detail


class RpcRefused(Exception):
    """The trader answered ok=False: it handled the request and refused it."""

    def __init__(self, code: str, message: str = "", details: Optional[dict] = None):
        super().__init__(f"{code}: {message}" if message else code)
        self.code, self.message, self.details = code, message, details


def _call_blocking(client: Any, method: str, body: dict, timeout: float, options: dict) -> dict:
    from trader.messaging.typed_rpc import TypedRpcRemoteError
    try:
        return client.call(method, body, dict, timeout, **options)
    except TypedRpcRemoteError as exc:
        raise RpcRefused(exc.code, exc.message, exc.details) from None
    except ConnectionError as exc:
        # TypedRpcClient.call raises ConnectionError only before the send: no socket, or IMMEDIATE=1 refused.
        raise RpcNotSent("TRADER_UNREACHABLE", str(exc)) from None
    except TimeoutError as exc:
        raise RpcOutcomeUnknown("REPLY_TIMEOUT", str(exc)) from None
    except Exception as exc:                       # a reply that failed verification, or anything after the send
        raise RpcOutcomeUnknown("REPLY_UNTRUSTED", type(exc).__name__) from None


class PrincipalClient:
    def __init__(self, principal: str, *, command: Any, query: Any, commands: frozenset[str],
                 queries: frozenset[str], timeout: float, slow_query: Any = None,
                 slow_queries: Optional[Mapping[str, float]] = None):
        self.principal = principal
        slow = dict(slow_queries or {})
        if slow and slow_query is None:
            raise ValueError("slow queries need their own socket")
        self._routes = {**{m: command for m in commands}, **{m: query for m in queries},
                        **{m: slow_query for m in slow}}
        self._timeouts = slow
        self._timeout = timeout
        self._epoch: Callable[[], Optional[int]] = lambda: None
        self._sockets = [s for s in (command, query, slow_query) if s is not None]

    @property
    def methods(self) -> frozenset[str]:
        return frozenset(self._routes)

    def bind_epoch(self, source: Callable[[], Optional[int]]) -> None:
        self._epoch = source

    async def call(self, method: str, body: dict, *, epoch: Optional[int] = None) -> dict:
        client = self._routes.get(method)
        if client is None:
            raise MethodNotAllowedLocally("METHOD_NOT_IN_CLIENT_SET", f"{self.principal} may not call {method}")
        options: dict = {}
        if method in EPOCH_METHODS:
            held = epoch if epoch is not None else self._epoch()
            if held is None:
                raise RpcNotSent("NOT_LEADER", f"{method} needs a held controller epoch")
            options["controller_epoch"] = held
        timeout = self._timeouts.get(method, self._timeout)
        return await asyncio.to_thread(_call_blocking, client, method, body, timeout, options)

    def close(self) -> None:
        for socket in self._sockets:
            socket.close()


class ReadOnlySupervisor:
    """What a DecisionEngine may ask the trader: queries only, never a command, never an epoch method."""

    def __init__(self, supervisor: PrincipalClient):
        self._supervisor = supervisor

    async def call(self, method: str, body: dict) -> dict:
        if method not in ENGINE_QUERIES:
            raise MethodNotAllowedLocally("METHOD_NOT_FOR_ENGINES", f"an engine may not call {method}")
        return await self._supervisor.call(method, body)


@dataclass
class AiRpcClients:
    supervisor: PrincipalClient
    research: PrincipalClient

    @classmethod
    def from_sockets(cls, *, supervisor_command: Any, supervisor_query: Any, supervisor_discovery: Any,
                     research_command: Any, research_query: Any, timeout: float) -> "AiRpcClients":
        supervisor = PrincipalClient("ai_supervisor", command=supervisor_command, query=supervisor_query,
                                     slow_query=supervisor_discovery, commands=SUPERVISOR_COMMANDS,
                                     queries=SUPERVISOR_QUERIES, slow_queries=SUPERVISOR_SLOW_QUERIES,
                                     timeout=timeout)
        research = PrincipalClient("ai_research", command=research_command, query=research_query,
                                   commands=RESEARCH_COMMANDS, queries=RESEARCH_QUERIES, timeout=timeout)
        return cls(supervisor, research)

    @classmethod
    def connect(cls, *, keys_dir: Optional[str], address: str, query_port: int, command_port: int,
                timeout: float) -> "AiRpcClients":
        """Load both key pairs (startup only) and open five sockets. ZMQ connects lazily: no trader needed yet."""
        from trader.messaging.typed_rpc import ServiceIdentity, TypedRpcClient

        identities = {p: ServiceIdentity.load(p, keys_dir) for p in ("ai_supervisor", "ai_research")}

        def socket(principal: str, role: str) -> Any:
            port = command_port if role == "command" else query_port
            client = TypedRpcClient(role, identities[principal], server="trader", address=address, port=port,
                                    timeout=timeout)
            client.connect()
            return client
        return cls.from_sockets(
            supervisor_command=socket("ai_supervisor", "command"), supervisor_query=socket("ai_supervisor", "query"),
            supervisor_discovery=socket("ai_supervisor", "query"),
            research_command=socket("ai_research", "command"), research_query=socket("ai_research", "query"),
            timeout=timeout)

    def close(self) -> None:
        self.supervisor.close()
        self.research.close()
```

- [ ] **Step 4: Run**

Run: `.venv/bin/python -m pytest tests/ai/runtime/test_rpc_clients.py -q --timeout=60`
Expected: all pass. If `TypedRpcRegistry.register` needs a different keyword for the caller in this base, use the form Plan 1's `tests/test_typed_rpc_controller_epoch.py::test_the_client_sends_the_epoch_and_the_handler_reads_it` uses.

- [ ] **Step 5: Commit**

```bash
git add trader/ai/rpc_clients.py tests/ai/runtime/fakes.py tests/ai/runtime/test_rpc_clients.py
git commit -m "feat: add method-restricted ai supervisor and research rpc clients

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 3: Leadership: grant, renew, loss, restart wait

**Files:**
- Create: `trader/ai/leadership.py`
- Modify (tests): `tests/ai/runtime/fakes.py` (add `EpochTrader`)
- Create (tests): `tests/ai/runtime/test_leadership.py`

**Interfaces:**
- Consumes: `PrincipalClient.call` (Task 2), `RpcNotSent`, `RpcOutcomeUnknown`, `RpcRefused`; Plan 1 `HOLDER_ID`, `ControllerEpochs`, `EpochRefused`, `apply_controller_epoch_migration`; `AiStore`; `Clock`.
- Produces: `LEASE_SAFETY_SECONDS = 5.0`; `new_holder_id() -> str`; `NotLeader(code)`; `HeldEpoch(epoch, lease_expires_at, local_deadline)`; `Leadership(*, supervisor, store, clock, holder_id, lease_seconds=60, renew_seconds=20, held_retry_seconds=5.0)` with `holder_id`, `last_epoch`, `current_epoch() -> Optional[int]`, `async grant_once() -> int`, `async acquire(stop=None) -> Optional[int]`, `async run_renewals(stop)`, `async mark_lost(reason)`, `async on_stale(code)`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/ai/runtime/fakes.py`:

```python
class EpochTrader:
    """The supervisor's grant served by Plan 1's real ControllerEpochs on a temp journal (trader time = clock)."""

    def __init__(self, directory, clock):
        from trader.automation.controller_epoch import ControllerEpochs, apply_controller_epoch_migration
        from trader.data.domain_journal import DomainJournal
        from trader.data.duckdb_store import DuckDBConnection
        from trader.data.schema_migrations import SchemaMigrator

        directory.mkdir(parents=True, exist_ok=True)
        db = DuckDBConnection.get_instance(str(directory / "journal.duckdb"))
        migrator = SchemaMigrator(db)
        journal = DomainJournal(db)
        journal.migrate(migrator)
        apply_controller_epoch_migration(migrator)
        self.epochs = ControllerEpochs(journal=journal, now=clock.now)
        self.down = False
        self.calls = []

    async def call(self, method, body, *, epoch=None):
        from trader.ai.rpc_clients import RpcNotSent, RpcRefused
        from trader.automation.controller_epoch import EpochRefused

        assert method == "grant_ai_controller_epoch"
        self.calls.append(dict(body))
        if self.down:
            raise RpcNotSent("TRADER_UNREACHABLE")
        try:
            grant = self.epochs.grant(**body)
        except EpochRefused as exc:
            raise RpcRefused(exc.code, exc.message) from None
        return {"epoch": grant.epoch, "lease_expires_at": grant.lease_expires_at.isoformat()}
```

```python
# tests/ai/runtime/test_leadership.py
"""SP2 Plan 5 Task 3: one trader-granted epoch per process (spec 5.1, Plan 1 Rulings 2-3)."""
import pytest
import pytest_asyncio

from tests.ai.fakes import FakeClock
from tests.ai.runtime.fakes import EpochTrader, et
from trader.ai.leadership import Leadership, NotLeader, new_holder_id
from trader.ai.runtime_schema import ALL_MIGRATIONS
from trader.ai.store import AiStore
from trader.automation.controller_epoch import HOLDER_ID


@pytest.fixture
def clock():
    return FakeClock(et(11, 0))


@pytest.fixture
def store(tmp_path, clock):
    created = AiStore(tmp_path / "ai.duckdb", clock=clock)
    created.migrate(ALL_MIGRATIONS)
    return created


@pytest_asyncio.fixture
async def trader(tmp_path, clock):
    return EpochTrader(tmp_path / "trader", clock)


def leader(trader, store, clock):
    return Leadership(supervisor=trader, store=store, clock=clock, holder_id=new_holder_id())


def held_rows(store):
    return store.db.execute("SELECT epoch, holder_id, lost_reason FROM ai_held_epochs ORDER BY epoch", fetch="all")


@pytest.mark.asyncio
async def test_the_first_grant_is_persisted_before_it_is_used(trader, store, clock):
    a = leader(trader, store, clock)
    assert a.current_epoch() is None
    assert await a.acquire() == 1 and a.current_epoch() == 1
    assert held_rows(store) == [(1, a.holder_id, None)]


@pytest.mark.asyncio
async def test_renewal_keeps_the_epoch_and_moves_the_local_deadline(trader, store, clock):
    a = leader(trader, store, clock)
    await a.acquire()
    clock.advance(20)
    assert await a.grant_once() == 1
    clock.advance(50)
    assert a.current_epoch() == 1                      # renewed at +20: deadline is +75
    assert trader.calls[-1]["current_epoch"] == 1


@pytest.mark.asyncio
async def test_a_restart_waits_for_the_old_lease(trader, store, clock):          # Plan 1 Ruling 3
    a = leader(trader, store, clock)
    await a.acquire()
    b = leader(trader, store, clock)                   # the restarted process: a new holder
    started = clock.now()
    assert await b.acquire() == 2
    waited = (clock.now() - started).total_seconds()
    assert 60 <= waited <= 66
    with pytest.raises(NotLeader) as exc:
        await a.grant_once()
    assert exc.value.code == "CONTROLLER_EPOCH_HELD" and a.current_epoch() is None
    assert [(e, r) for e, _, r in held_rows(store)] == [(1, "CONTROLLER_EPOCH_HELD"), (2, None)]


@pytest.mark.asyncio
async def test_a_stale_reply_drops_the_epoch_at_once(trader, store, clock):
    a = leader(trader, store, clock)
    await a.acquire()
    await a.on_stale("CONTROLLER_EPOCH_STALE")
    assert a.current_epoch() is None
    assert held_rows(store)[0][2] == "CONTROLLER_EPOCH_STALE"


@pytest.mark.asyncio
async def test_without_renewal_the_epoch_ends_at_the_local_deadline(trader, store, clock):
    a = leader(trader, store, clock)
    await a.acquire()
    trader.down = True
    clock.advance(54.9)
    assert a.current_epoch() == 1
    clock.advance(0.1)                                 # 60 s lease minus the 5 s safety margin
    assert a.current_epoch() is None


@pytest.mark.asyncio
async def test_regaining_after_expiry_takes_the_next_epoch(trader, store, clock):
    a = leader(trader, store, clock)
    await a.acquire()
    clock.advance(61)
    assert await a.grant_once() == 2                   # Plan 1: an expired holder renewing gets a new epoch
    assert [(e, r) for e, _, r in held_rows(store)] == [(1, "SUPERSEDED"), (2, None)]


@pytest.mark.asyncio
async def test_an_epoch_the_trader_never_granted_is_dropped_and_asked_fresh(tmp_path, store, clock):
    a = Leadership(supervisor=EpochTrader(tmp_path / "t1", clock), store=store, clock=clock,
                   holder_id=new_holder_id())
    await a.acquire()
    a._supervisor = EpochTrader(tmp_path / "t2", clock)        # the trader's journal was reset
    with pytest.raises(NotLeader) as exc:
        await a.grant_once()
    assert exc.value.code == "CONTROLLER_EPOCH_UNKNOWN" and a.last_epoch is None
    assert await a.grant_once() == 1


@pytest.mark.asyncio
async def test_acquire_stops_when_asked(trader, store, clock):
    import asyncio
    a = leader(trader, store, clock)
    await a.acquire()
    stop = asyncio.Event()
    stop.set()
    assert await leader(trader, store, clock).acquire(stop) is None


def test_holder_ids_are_fresh_per_process():
    first, second = new_holder_id(), new_holder_id()
    assert first != second and HOLDER_ID.fullmatch(first) and first.startswith("ai-")
```

- [ ] **Step 2: Run them and see them fail**

Run: `.venv/bin/python -m pytest tests/ai/runtime/test_leadership.py -q --timeout=60`
Expected: `ModuleNotFoundError: No module named 'trader.ai.leadership'`.

- [ ] **Step 3: Implement**

```python
# trader/ai/leadership.py
"""Controller leadership: one trader-granted epoch per process (SP2 spec 5.1, Plan 5 Rulings 4-5).

The trader decides who leads (Plan 1). This side only asks, persists what it
got before using it, and stops using it at once when the trader says another
holder leads, when a command is refused as stale, or when its own conservative
deadline passes without a renewal.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import logging
import uuid
from dataclasses import dataclass
from typing import Any, Optional

from trader.ai.rpc_clients import RpcNotSent, RpcOutcomeUnknown, RpcRefused

logger = logging.getLogger(__name__)

LEASE_SAFETY_SECONDS = 5.0
LOST_TO_ANOTHER = frozenset({"CONTROLLER_EPOCH_HELD", "CONTROLLER_EPOCH_UNKNOWN"})


def new_holder_id() -> str:
    """Fresh per process: a restart is a new holder and waits for the old lease (Plan 1 Ruling 3)."""
    return f"ai-{uuid.uuid4().hex[:12]}"


class NotLeader(Exception):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class HeldEpoch:
    epoch: int
    lease_expires_at: dt.datetime          # the trader's clock; audit only
    local_deadline: float                  # our monotonic clock; the epoch is used only before it


def _parse_grant(reply: Any) -> tuple[int, dt.datetime]:
    epoch = reply.get("epoch") if isinstance(reply, dict) else None
    text = reply.get("lease_expires_at") if isinstance(reply, dict) else None
    if type(epoch) is not int or epoch < 1 or not isinstance(text, str):
        raise RpcOutcomeUnknown("GRANT_REPLY_MALFORMED")
    try:
        expires = dt.datetime.fromisoformat(text)
    except ValueError:
        raise RpcOutcomeUnknown("GRANT_REPLY_MALFORMED") from None
    if expires.utcoffset() is None:
        raise RpcOutcomeUnknown("GRANT_REPLY_MALFORMED")
    return epoch, expires.astimezone(dt.timezone.utc)


class Leadership:
    def __init__(self, *, supervisor: Any, store: Any, clock: Any, holder_id: str, lease_seconds: int = 60,
                 renew_seconds: float = 20, held_retry_seconds: float = 5.0):
        from trader.automation.controller_epoch import HOLDER_ID
        if not HOLDER_ID.fullmatch(holder_id):
            raise ValueError("holder_id must match the trader's HOLDER_ID pattern")
        self._supervisor, self._store, self._clock = supervisor, store, clock
        self._holder_id = holder_id
        self._lease_seconds, self._renew_seconds, self._held_retry = lease_seconds, renew_seconds, held_retry_seconds
        self._held: Optional[HeldEpoch] = None
        self._last_epoch: Optional[int] = None

    @property
    def holder_id(self) -> str:
        return self._holder_id

    @property
    def last_epoch(self) -> Optional[int]:
        return self._last_epoch

    def current_epoch(self) -> Optional[int]:
        """The epoch to send now, or None. Callers check it right before every epoch call."""
        held = self._held
        if held is None or self._clock.monotonic() >= held.local_deadline:
            return None
        return held.epoch

    async def grant_once(self) -> int:
        started = self._clock.monotonic()          # before the request: the trader's lease starts later
        body = {"holder_id": self._holder_id, "current_epoch": self._last_epoch,
                "lease_seconds": self._lease_seconds}
        try:
            reply = await self._supervisor.call("grant_ai_controller_epoch", body)
        except RpcRefused as exc:
            if exc.code not in LOST_TO_ANOTHER:
                raise                              # PERMISSION_DENIED and friends: a wrong key, fail loudly
            if exc.code == "CONTROLLER_EPOCH_UNKNOWN":
                self._last_epoch = None            # this trader never granted it (reset journal): ask fresh
            await self.mark_lost(exc.code)
            raise NotLeader(exc.code) from None
        epoch, expires = _parse_grant(reply)
        await self._persist(epoch, expires)
        self._last_epoch = epoch
        self._held = HeldEpoch(epoch, expires, started + self._lease_seconds - LEASE_SAFETY_SECONDS)
        return epoch

    async def _persist(self, epoch: int, expires: dt.datetime) -> None:
        now, previous, holder = self._clock.now(), self._last_epoch, self._holder_id

        def work(conn: Any) -> None:
            if previous is not None and previous != epoch:
                conn.execute("UPDATE ai_held_epochs SET lost_at = ?, lost_reason = 'SUPERSEDED' "
                             "WHERE epoch = ? AND lost_at IS NULL", [now, previous])
            if conn.execute("SELECT 1 FROM ai_held_epochs WHERE epoch = ?", [epoch]).fetchone():
                conn.execute("UPDATE ai_held_epochs SET holder_id = ?, renewed_at = ?, lease_expires_at = ?, "
                             "lost_at = NULL, lost_reason = NULL WHERE epoch = ?", [holder, now, expires, epoch])
            else:
                conn.execute("INSERT INTO ai_held_epochs VALUES (?, ?, ?, ?, ?, NULL, NULL)",
                             [epoch, holder, now, now, expires])
        await self._store.atransaction(work)

    async def mark_lost(self, reason: str) -> None:
        held, self._held = self._held, None        # stop using it before anything else happens
        if held is None:
            return
        logger.warning("controller epoch %s lost: %s", held.epoch, reason)
        now = self._clock.now()
        await self._store.atransaction(lambda conn: conn.execute(
            "UPDATE ai_held_epochs SET lost_at = ?, lost_reason = ? WHERE epoch = ? AND lost_at IS NULL",
            [now, reason, held.epoch]))

    async def on_stale(self, code: str) -> None:
        """The trader refused this process's epoch on a command or read."""
        await self.mark_lost(code)

    async def acquire(self, stop: Optional[asyncio.Event] = None) -> Optional[int]:
        """Ask until granted; a restarted process waits here up to one lease. None when stopped."""
        while stop is None or not stop.is_set():
            try:
                return await self.grant_once()
            except NotLeader:
                pass
            except (RpcNotSent, RpcOutcomeUnknown) as exc:
                logger.warning("controller epoch grant failed (%s); retrying", exc.code)
            await self._clock.sleep(self._held_retry)
        return None

    async def run_renewals(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            held = self._held
            if held is not None and self._clock.monotonic() >= held.local_deadline:
                await self.mark_lost("LEASE_EXPIRED_LOCALLY")
            await self._clock.sleep(self._renew_seconds if self.current_epoch() is not None else self._held_retry)
            try:
                await self.grant_once()
            except NotLeader:
                continue
            except (RpcNotSent, RpcOutcomeUnknown) as exc:
                logger.warning("controller epoch renewal failed (%s); the epoch stays until its local deadline",
                               exc.code)
```

- [ ] **Step 4: Run**

Run: `.venv/bin/python -m pytest tests/ai/runtime/test_leadership.py -q --timeout=60`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add trader/ai/leadership.py tests/ai/runtime/fakes.py tests/ai/runtime/test_leadership.py
git commit -m "feat: hold the ai controller epoch with renewal and loss handling

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 4: Session-aligned slots

**Files:**
- Create: `trader/ai/schedule.py`
- Create (tests): `tests/ai/runtime/test_schedule.py`

**Interfaces:**
- Consumes: `XNYSCalendarPolicy.resolve(now)`, `XNYSCalendarPolicy.allows_new_entry(now)`, `SessionSchedule` (`trader/automation/calendar_policy.py`).
- Produces: `ENTRY = "entry"`, `POSITION = "position"`; `Slot(kind, session_date, start, deadline)` with `cycle_id`; `SessionSlots(*, calendar=None, entry_minutes=15, position_minutes=15, grace_seconds=120)` with `latest(kind, now) -> Optional[Slot]`, `is_due(slot, now) -> bool`, `entry_window_open(now) -> bool`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/ai/runtime/test_schedule.py
"""SP2 Plan 5 Task 4: XNYS slots for entry and position cycles (spec 5.2, Rulings 6-8)."""
import datetime as dt

from tests.ai.runtime.fakes import et
from trader.ai.schedule import ENTRY, POSITION, SessionSlots

EARLY_CLOSE = dt.date(2026, 11, 27)          # the day after Thanksgiving: XNYS closes at 13:00
SATURDAY = dt.date(2026, 7, 18)


def test_entry_slots_run_from_after_stabilization_to_before_the_cutoff():
    slots = SessionSlots()
    assert slots.latest(ENTRY, et(9, 40)) is None                       # 09:30 is inside the stabilization
    first = slots.latest(ENTRY, et(9, 46))
    assert (first.cycle_id, first.start, first.deadline) == ("cyc-entry-20260717-0945", et(9, 45), et(10, 0))
    last = slots.latest(ENTRY, et(15, 29))
    assert (last.cycle_id, last.deadline) == ("cyc-entry-20260717-1515", et(15, 30))
    late = slots.latest(ENTRY, et(15, 40))
    assert late.cycle_id.endswith("1515") and not slots.is_due(late, et(15, 40))


def test_position_slots_continue_after_the_entry_cutoff():
    slots = SessionSlots()
    slot = slots.latest(POSITION, et(15, 31))
    assert (slot.cycle_id, slot.deadline) == ("cyc-position-20260717-1530", et(15, 45))
    assert slots.is_due(slot, et(15, 31)) and not slots.entry_window_open(et(15, 31))


def test_early_close_moves_both_windows():
    slots = SessionSlots()
    entry = slots.latest(ENTRY, et(12, 29, day=EARLY_CLOSE))
    position = slots.latest(POSITION, et(12, 31, day=EARLY_CLOSE))
    assert (entry.cycle_id, entry.deadline) == ("cyc-entry-20261127-1215", et(12, 30, day=EARLY_CLOSE))
    assert (position.cycle_id, position.deadline) == ("cyc-position-20261127-1230", et(12, 45, day=EARLY_CLOSE))


def test_no_slot_and_no_entry_window_on_a_closed_day():
    slots = SessionSlots()
    assert slots.latest(ENTRY, et(11, 0, day=SATURDAY)) is None
    assert slots.latest(POSITION, et(11, 0, day=SATURDAY)) is None
    assert not slots.entry_window_open(et(11, 0, day=SATURDAY))


def test_slot_ids_use_new_york_time_across_dst():
    slots = SessionSlots()
    before = slots.latest(ENTRY, et(9, 50, day=dt.date(2026, 3, 6)))      # EST
    after = slots.latest(ENTRY, et(9, 50, day=dt.date(2026, 3, 9)))       # EDT
    assert before.cycle_id.endswith("0945") and after.cycle_id.endswith("0945")
    assert before.start.hour - after.start.hour == 1                      # UTC hour moves, the id does not


def test_a_slot_is_due_only_inside_its_start_grace():
    slots = SessionSlots(grace_seconds=120)
    slot = slots.latest(ENTRY, et(11, 0))
    assert slots.is_due(slot, et(11, 1, 59)) and not slots.is_due(slot, et(11, 2, 0))


def test_a_shorter_interval_keeps_the_same_alignment():
    slot = SessionSlots(entry_minutes=5).latest(ENTRY, et(9, 36))
    assert slot.cycle_id.endswith("0935") and slot.deadline == et(9, 40)
```

- [ ] **Step 2: Run them and see them fail**

Run: `.venv/bin/python -m pytest tests/ai/runtime/test_schedule.py -q --timeout=60`
Expected: `ModuleNotFoundError: No module named 'trader.ai.schedule'`.

- [ ] **Step 3: Implement**

```python
# trader/ai/schedule.py
"""Session-aligned slots for entry and position cycles (SP2 spec 5.2, Plan 5 Rulings 6-8).

Slots start at open + k x interval. Entry slots count inside SP1's entry window
(after the opening stabilization, before the cutoff 30 min before the close);
position slots run on to the session flatten start. Early closes follow from
the calendar, because SP1 derives every deadline from the official close.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Any, Optional
from zoneinfo import ZoneInfo

from trader.automation.calendar_policy import XNYSCalendarPolicy

ET = ZoneInfo("America/New_York")
ENTRY = "entry"
POSITION = "position"


@dataclass(frozen=True)
class Slot:
    kind: str
    session_date: dt.date
    start: dt.datetime
    deadline: dt.datetime                 # the cycle's work is cancelled here (Ruling 8)

    @property
    def cycle_id(self) -> str:
        return f"cyc-{self.kind}-{self.session_date:%Y%m%d}-{self.start.astimezone(ET):%H%M}"


class SessionSlots:
    def __init__(self, *, calendar: Optional[Any] = None, entry_minutes: int = 15, position_minutes: int = 15,
                 grace_seconds: int = 120):
        self._calendar = calendar if calendar is not None else XNYSCalendarPolicy()
        self._interval = {ENTRY: dt.timedelta(minutes=entry_minutes), POSITION: dt.timedelta(minutes=position_minutes)}
        self._grace = dt.timedelta(seconds=grace_seconds)

    def entry_window_open(self, now: dt.datetime) -> bool:
        return self._calendar.allows_new_entry(now)

    def latest(self, kind: str, now: dt.datetime) -> Optional[Slot]:
        """The newest slot of ``kind`` that started at or before ``now`` today, or None."""
        schedule = self._calendar.resolve(now)
        if schedule is None or now < schedule.open_utc:
            return None
        interval = self._interval[kind]
        newest = int((now - schedule.open_utc) / interval)
        for k in range(newest, -1, -1):
            start = schedule.open_utc + k * interval
            end = schedule.entry_cutoff_utc if kind == ENTRY else schedule.flatten_start_utc
            if schedule.opening_stabilization_end_utc <= start < end:
                return Slot(kind, schedule.session_date, start, min(start + interval, end))
        return None

    def is_due(self, slot: Slot, now: dt.datetime) -> bool:
        return slot.start <= now < min(slot.start + self._grace, slot.deadline)
```

- [ ] **Step 4: Run**

Run: `.venv/bin/python -m pytest tests/ai/runtime/test_schedule.py -q --timeout=60`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add trader/ai/schedule.py tests/ai/runtime/test_schedule.py
git commit -m "feat: add xnys session-aligned entry and position slots

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 5: The `DecisionEngine` contract, ids and a scripted fake engine

**Files:**
- Create: `trader/ai/ids.py`, `trader/ai/engine.py`
- Create (tests): `tests/ai/runtime/scripted_engine.py`, `tests/ai/runtime/test_engine_contract.py`

**Interfaces:**
- Consumes: Plan 4 `ROLE_NAMES`, `ModelCaller`, `DecisionDeadline`; `Slot` (Task 4).
- Produces: everything listed for `trader.ai.ids` and `trader.ai.engine` in Cross-plan additions; `ENTRY_ACTIONS = frozenset({"ENTER"})`, `EXIT_ACTIONS = frozenset({"CLOSE", "PARTIAL_CLOSE"})`, `ALLOWED_ACTIONS: Mapping[source_kind, frozenset]`; test helper `ScriptedEngine`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/ai/runtime/scripted_engine.py
"""A deterministic DecisionEngine for the Plan 5 tests: fixed results per hook, every call recorded."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any, Callable, Optional, Union

from trader.ai.engine import EngineResult, ProposedDecision

Scripted = Union[EngineResult, Callable[[Any], EngineResult]]
HOOKS = ("entry_signal", "exit_signal", "entry_cycle", "position_cycle")


class ScriptedEngine:
    def __init__(self, *, block: Optional[asyncio.Event] = None, error: Optional[Exception] = None,
                 **results: Scripted):
        unknown = set(results) - set(HOOKS)
        if unknown:
            raise ValueError(f"unknown hooks {sorted(unknown)}")
        self.results = {hook: results.get(hook, EngineResult()) for hook in HOOKS}
        self.block, self.error = block, error
        self.calls: list[tuple[str, Any]] = []

    @classmethod
    def from_file(cls, path: str) -> "ScriptedEngine":
        """JSON {"entry_signal": [ProposedDecision fields, ...], ...} for the child-process test."""
        raw = json.loads(Path(path).read_text())
        return cls(**{hook: EngineResult(decisions=tuple(ProposedDecision(**d) for d in decisions))
                      for hook, decisions in raw.items()})

    async def _run(self, hook: str, ctx: Any) -> EngineResult:
        self.calls.append((hook, ctx))
        if self.block is not None:
            await self.block.wait()
        if self.error is not None:
            raise self.error
        result = self.results[hook]
        return result(ctx) if callable(result) else result

    async def on_entry_signal(self, ctx):
        return await self._run("entry_signal", ctx)

    async def on_exit_signal(self, ctx):
        return await self._run("exit_signal", ctx)

    async def on_entry_cycle(self, ctx):
        return await self._run("entry_cycle", ctx)

    async def on_position_cycle(self, ctx):
        return await self._run("position_cycle", ctx)

    def hooks_called(self) -> list[str]:
        return [hook for hook, _ in self.calls]
```

```python
# tests/ai/runtime/test_engine_contract.py
"""SP2 Plan 5 Task 5: the DecisionEngine contract and code-owned ids (spec 5.3, 8)."""
import dataclasses
import datetime as dt
import math

import pytest

from tests.ai.fakes import FakeClock
from tests.ai.runtime.fakes import AAPL, et
from tests.ai.runtime.scripted_engine import ScriptedEngine
from trader.ai.engine import (
    DecisionEngine, ExperimentView, ModelWork, ProposedDecision, SimulatedBaseline, owned_positions_from_trips,
)
from trader.ai.gateway import DecisionDeadline
from trader.ai.ids import derive_decision_id

SIG = "sig-" + "1" * 32
EVIDENCE = "sha256:" + "c" * 64


def enter(**changes):
    base = dict(action_key=f"enter:{AAPL}", action="ENTER", conid=AAPL, side="BUY", decider="jev",
                evidence_digest=EVIDENCE, deployment_digest="sha256:" + "a" * 64, policy_revision=1,
                stop_price=225.4, target_price=234.6, quantity=3)
    base.update(changes)
    return ProposedDecision(**base)


def test_decision_ids_are_derived_from_source_and_action_identity():
    first = derive_decision_id(SIG, f"enter:{AAPL}")
    assert first == derive_decision_id(SIG, f"enter:{AAPL}")                  # a redelivery is the same decision
    assert first != derive_decision_id(SIG, "enter:272093") != derive_decision_id("cyc-entry-20260717-1100",
                                                                                   f"enter:{AAPL}")
    assert first.startswith("dec-") and len(first) == 36


def test_command_id_matches_the_trader_rule():
    from trader.ai.ids import command_id_for
    from trader.automation.ai_paper_decision import command_id_for as trader_rule
    decision_id = derive_decision_id(SIG, f"enter:{AAPL}")
    assert command_id_for(decision_id) == trader_rule(decision_id)


@pytest.mark.parametrize("changes", [
    {"action_key": "Enter:1"}, {"action_key": "enter"}, {"action": "BUY"}, {"conid": True}, {"conid": 0},
    {"side": "SHORT"}, {"quantity": 0}, {"quantity": 2.0}, {"stop_price": math.nan}, {"stop_price": -1.0},
    {"evidence_digest": "abc"}, {"decider": "Jev"}, {"policy_revision": True}])
def test_a_proposed_decision_refuses_bad_values(changes):
    with pytest.raises(ValueError):
        enter(**changes)


def test_baselines_follow_the_index_pairs_and_take_at_most_one_link():
    now = et(11, 0)
    SimulatedBaseline("no_trade.v1", "self_found", "cyc-entry-20260717-1100", now)
    with pytest.raises(ValueError):
        SimulatedBaseline("no_trade.v1", "strategy_signal", "x", now)
    with pytest.raises(ValueError):
        SimulatedBaseline("follow_signal.v1", "strategy_signal", SIG, now, linked_action_key=f"enter:{AAPL}",
                          linked_decision_id="dec-" + "0" * 32)
    with pytest.raises(ValueError):
        SimulatedBaseline("no_trade.v1", "self_found", "has space", now)


@pytest.mark.parametrize("fields", [
    dict(quantity=3),                                              # the trader sizes follow_signal (Plan 2 R19)
    dict(deployment_digest=None),
    dict(incomplete_reason="quote_unavailable"),                   # an incomplete record has no prices
    dict(linked_round_trip_id="rt-1"),                             # matched-entry only
])
def test_baselines_take_plan_2_shapes(fields):
    base = dict(conid=AAPL, side="BUY", reference_price=230.0, stop_price=225.4, target_price=234.6,
                deployment_digest="sha256:" + "a" * 64)
    SimulatedBaseline("follow_signal.v1", "strategy_signal", SIG, et(11, 0), **base)          # the valid shape
    with pytest.raises(ValueError):
        SimulatedBaseline("follow_signal.v1", "strategy_signal", SIG, et(11, 0), **{**base, **fields})
    SimulatedBaseline("follow_signal.v1", "strategy_signal", SIG, et(11, 0), conid=AAPL,
                      incomplete_reason="feed_not_accepted")                                     # incomplete shape


@pytest.mark.asyncio
async def test_model_work_registers_its_context_and_owns_request_keys():
    registered = []

    async def register(key, kind, served_id):
        registered.append((key, kind, served_id))
    clock = FakeClock(et(11, 0))
    work = ModelWork(context_key=SIG, served_kind="signal", served_id=SIG, source_id=SIG,
                     experiment_id="exp-" + "0" * 20, gateway=object(),
                     deadline=DecisionDeadline(clock, 60), register=register)
    assert work.request_key("orchestrator", 1) == f"{SIG}/orchestrator/1"
    with pytest.raises(ValueError):
        work.request_key("judge", 1)
    child = await work.for_action(f"enter:{AAPL}")
    decision_id = derive_decision_id(SIG, f"enter:{AAPL}")
    assert (child.context_key, child.served_kind, child.deadline) == (decision_id, "decision", work.deadline)
    assert registered == [(decision_id, "decision", decision_id)]
    assert child.request_key("jev", 1) == f"{decision_id}/jev/1"


def test_experiment_view_reads_the_trader_reply_and_refuses_bad_shapes():
    reply = {"experiment": {"experiment_id": "exp-" + "a" * 20, "state": "ARMED",
                            "started_at": "2026-07-17T15:00:00+00:00"}, "entry_block": None}
    view = ExperimentView.from_reply(reply)
    assert (view.state, view.started_at, view.entry_block) == ("ARMED", et(11, 0), None)
    assert ExperimentView.from_reply({"experiment": None, "entry_block": None}) is None
    for bad in ({"experiment": {**reply["experiment"], "state": "RUNNING"}, "entry_block": None},
                {"experiment": {**reply["experiment"], "started_at": "2026-07-17T15:00:00"}, "entry_block": None},
                {"experiment": {**reply["experiment"], "experiment_id": "exp-1"}, "entry_block": None}):
        with pytest.raises(ValueError):
            ExperimentView.from_reply(bad)


def test_owned_positions_are_the_open_trips_with_quantity_left():
    trip = {"round_trip_id": "rt-1", "conid": AAPL, "symbol": "AAPL", "direction": "LONG",
            "opened_at": "2026-07-17T15:05:00+00:00", "closed_at": None, "opened_quantity": 3.0,
            "closed_quantity": 1.0, "decision_id": "dec-" + "1" * 32, "state": "OPEN"}
    closed = {**trip, "round_trip_id": "rt-2", "state": "CLOSED"}
    positions = owned_positions_from_trips({"experiment_id": "exp-" + "a" * 20, "trips": [trip, closed]})
    assert [(p.round_trip_id, p.open_quantity) for p in positions] == [("rt-1", 2.0)]
    with pytest.raises(ValueError):
        owned_positions_from_trips({"experiment_id": "x", "error_code": "EXPERIMENT_NOT_FOUND", "trips": None})


def test_the_scripted_engine_satisfies_the_protocol():
    engine: DecisionEngine = ScriptedEngine()
    assert all(callable(getattr(engine, name)) for name in
               ("on_entry_signal", "on_exit_signal", "on_entry_cycle", "on_position_cycle"))
    assert dataclasses.is_dataclass(enter())
```

- [ ] **Step 2: Run them and see them fail**

Run: `.venv/bin/python -m pytest tests/ai/runtime/test_engine_contract.py -q --timeout=60`
Expected: `ModuleNotFoundError: No module named 'trader.ai.engine'`.

- [ ] **Step 3: Implement**

```python
# trader/ai/ids.py
"""Code-owned identities (SP2 spec 5.3, 8; Plan 5 Rulings 11-13). Models never name an id."""
from __future__ import annotations

import hashlib
import json
import re
from typing import Any

ACTION_KEY = re.compile(r"^[a-z][a-z_]{0,15}:[0-9]{1,12}(:[a-z0-9_]{1,24})?$")


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def derive_decision_id(source_id: str, action_key: str) -> str:
    """Cycle id or signal source-event id plus the action identity: one id per logical decision."""
    if not isinstance(source_id, str) or not source_id:
        raise ValueError("source_id must be a non-empty string")
    if not isinstance(action_key, str) or not ACTION_KEY.fullmatch(action_key):
        raise ValueError("action_key must look like 'enter:265598'")
    return "dec-" + _sha(f"{source_id}|{action_key}")[:32]


def command_id_for(decision_id: str) -> str:
    """The trader's rule (trader.automation.ai_paper_decision.command_id_for); a test pins they agree."""
    return f"aip-{decision_id}"


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def cost_record_id(event_id: str) -> str:
    return "cost-" + _sha(event_id)[:40]


def attempt_ref(attempt_key: str) -> str:
    return "att-" + _sha(attempt_key)[:40]


def simulated_record_id(experiment_id: str, baseline_id: str, opportunity_id: str) -> str:
    return "sim-" + _sha(f"{experiment_id}|{baseline_id}|{opportunity_id}")[:40]
```

```python
# trader/ai/engine.py
"""The DecisionEngine contract between the controller (Plan 5) and the trading judgments (Plan 6).

The engine judges; the controller owns ids, expiry, persistence, submission and
reporting (spec 3, 8). Hooks never submit, never set an id and never raise for
a model failure: they return what they decided plus the baselines (spec 7).
"""
from __future__ import annotations

import datetime as dt
import math
import re
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Mapping, Optional, Protocol

from trader.ai.config import ROLE_NAMES
from trader.ai.ids import ACTION_KEY, derive_decision_id
from trader.ai.schedule import Slot

EXPERIMENT_STATES = ("ARMED", "PAUSED", "KILLED", "STOPPED")
ENTRY_ACTIONS = frozenset({"ENTER"})
EXIT_ACTIONS = frozenset({"CLOSE", "PARTIAL_CLOSE"})
ALLOWED_ACTIONS: Mapping[str, frozenset[str]] = {
    "entry_signal": ENTRY_ACTIONS, "entry_cycle": ENTRY_ACTIONS,
    "exit_signal": EXIT_ACTIONS, "position_cycle": EXIT_ACTIONS,
}
BASELINE_COHORTS: Mapping[str, str] = {
    "follow_signal.v1": "strategy_signal", "fixed_rule.v1": "self_found",
    "no_trade.v1": "self_found", "matched_entry_bracket_exit.v1": "model_close",
}
TRADER_SIZED_BASELINES = frozenset({"follow_signal.v1", "fixed_rule.v1"})     # Plan 2 Ruling 19
INCOMPLETE_REASONS = ("quote_unavailable", "feed_not_accepted", "budget_refused", "model_failed",
                      "sizing_unavailable")                                       # Plan 2 Ruling 18
_EXPERIMENT_ID = re.compile(r"^exp-[0-9a-f]{20}$")
_DECIDER = re.compile(r"^[a-z][a-z0-9_.-]{0,63}$")
_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_OPPORTUNITY_ID = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")
_DECISION_ID = re.compile(r"^[A-Za-z0-9_-]{8,64}$")


def parse_aware(text: Any, name: str) -> dt.datetime:
    if not isinstance(text, str):
        raise ValueError(f"{name} must be an ISO-8601 string")
    value = dt.datetime.fromisoformat(text)
    if value.utcoffset() is None:
        raise ValueError(f"{name} must carry a UTC offset")
    return value.astimezone(dt.timezone.utc)


def _price(value: Any, name: str) -> None:
    if value is not None and (type(value) is not float or not math.isfinite(value) or value <= 0):
        raise ValueError(f"{name} must be a positive finite float or None")


def _positive_int(value: Any, name: str) -> None:
    if value is not None and (type(value) is not int or value < 1):
        raise ValueError(f"{name} must be an integer >= 1 or None")


def _conid(value: Any) -> int:
    if type(value) is not int or value <= 0:
        raise ValueError("conid must be a positive integer")
    return value


@dataclass(frozen=True)
class ExperimentView:
    experiment_id: str
    state: str
    started_at: dt.datetime
    entry_block: Optional[str]

    @classmethod
    def from_reply(cls, reply: Any) -> Optional["ExperimentView"]:
        if not isinstance(reply, dict) or "experiment" not in reply:
            raise ValueError("get_experiment reply has no experiment key")
        experiment = reply["experiment"]
        if experiment is None:
            return None
        experiment_id, state = experiment.get("experiment_id"), experiment.get("state")
        if not isinstance(experiment_id, str) or not _EXPERIMENT_ID.fullmatch(experiment_id):
            raise ValueError("experiment_id must match exp-<20 hex>")
        if state not in EXPERIMENT_STATES:
            raise ValueError(f"unknown experiment state {state!r}")
        block = reply.get("entry_block")
        if block is not None and not isinstance(block, str):
            raise ValueError("entry_block must be a string or null")
        return cls(experiment_id, state, parse_aware(experiment.get("started_at"), "started_at"), block)


@dataclass(frozen=True)
class SignalOpportunity:
    opportunity_id: str              # the trader's source_event_id
    signal_cursor: int
    strategy_name: str
    conid: int
    action: str                      # BUY | SELL
    probability: Optional[float]
    signal_time: dt.datetime
    recorded_at: dt.datetime


@dataclass(frozen=True)
class OwnedPosition:
    round_trip_id: str
    conid: int
    symbol: str
    open_quantity: float
    opened_at: dt.datetime
    decision_id: Optional[str]


def owned_positions_from_trips(reply: Any) -> tuple[OwnedPosition, ...]:
    """Positions the experiment owns: OPEN trips with quantity left (get_experiment_trips, SP1 Plan 5)."""
    if not isinstance(reply, dict) or reply.get("error_code") or not isinstance(reply.get("trips"), list):
        raise ValueError(f"experiment trips unavailable: {reply.get('error_code') if isinstance(reply, dict) else None}")
    owned = []
    for trip in reply["trips"]:
        if trip.get("state") != "OPEN":
            continue
        left = float(trip["opened_quantity"]) - float(trip.get("closed_quantity") or 0.0)
        if left > 0:
            owned.append(OwnedPosition(str(trip["round_trip_id"]), _conid(trip["conid"]), str(trip["symbol"]), left,
                                       parse_aware(trip["opened_at"], "opened_at"), trip.get("decision_id")))
    return tuple(owned)


class ModelWork:
    """Identity and deadline of one piece of model work. The engine builds request keys only through it."""

    def __init__(self, *, context_key: str, served_kind: str, served_id: str, source_id: str, experiment_id: str,
                 gateway: Any, deadline: Any, register: Callable[[str, str, str], Awaitable[None]]):
        self.context_key, self.served_kind, self.served_id = context_key, served_kind, served_id
        self.source_id, self.experiment_id = source_id, experiment_id
        self.gateway, self.deadline = gateway, deadline
        self._register = register

    def request_key(self, role: str, call_seq: int) -> str:
        if role not in ROLE_NAMES or type(call_seq) is not int or call_seq < 1:
            raise ValueError("request keys need a known role and a call number >= 1")
        return f"{self.context_key}/{role}/{call_seq}"            # Plan 4 Ruling 10

    async def for_action(self, action_key: str) -> "ModelWork":
        """Work for one proposed action (e.g. its Jev call): costs link to the derived decision id."""
        decision_id = derive_decision_id(self.source_id, action_key)
        await self._register(decision_id, "decision", decision_id)
        return ModelWork(context_key=decision_id, served_kind="decision", served_id=decision_id,
                         source_id=self.source_id, experiment_id=self.experiment_id, gateway=self.gateway,
                         deadline=self.deadline, register=self._register)


@dataclass(frozen=True)
class SignalContext:
    now: dt.datetime
    experiment: ExperimentView
    opportunity: SignalOpportunity
    work: ModelWork


@dataclass(frozen=True)
class EntryCycleContext:
    now: dt.datetime
    experiment: ExperimentView
    slot: Slot
    work: ModelWork


@dataclass(frozen=True)
class PositionCycleContext:
    now: dt.datetime
    experiment: ExperimentView
    slot: Slot
    positions: tuple[OwnedPosition, ...]
    work: ModelWork


@dataclass(frozen=True)
class ProposedDecision:
    """One proposed trader command. The controller adds decision_id and expires_at (spec 8)."""
    action_key: str
    action: str
    conid: int
    side: str
    decider: str
    evidence_digest: str
    deployment_digest: Optional[str] = None
    policy_revision: Optional[int] = None
    stop_price: Optional[float] = None
    target_price: Optional[float] = None
    quantity: Optional[int] = None

    def __post_init__(self) -> None:
        if not isinstance(self.action_key, str) or not ACTION_KEY.fullmatch(self.action_key):
            raise ValueError("action_key must look like 'enter:265598'")
        if self.action not in ENTRY_ACTIONS | EXIT_ACTIONS:
            raise ValueError("action must be ENTER, CLOSE or PARTIAL_CLOSE")
        _conid(self.conid)
        if self.side not in ("BUY", "SELL"):
            raise ValueError("side must be BUY or SELL")
        if not isinstance(self.decider, str) or not _DECIDER.fullmatch(self.decider):
            raise ValueError("decider must match ^[a-z][a-z0-9_.-]{0,63}$")
        for name in ("evidence_digest", "deployment_digest"):
            value = getattr(self, name)
            if (value is not None or name == "evidence_digest") and (
                    not isinstance(value, str) or not _DIGEST.fullmatch(value)):
                raise ValueError(f"{name} must be sha256:<64 hex>")
        _positive_int(self.policy_revision, "policy_revision")
        _positive_int(self.quantity, "quantity")
        _price(self.stop_price, "stop_price")
        _price(self.target_price, "target_price")


@dataclass(frozen=True)
class SimulatedBaseline:
    """A baseline decision for the trader to simulate (spec 7; Plan 2 record_simulated_decision)."""
    baseline_id: str
    cohort: str
    opportunity_id: str
    decided_at: dt.datetime
    conid: Optional[int] = None
    side: Optional[str] = None
    quantity: Optional[int] = None
    reference_price: Optional[float] = None
    stop_price: Optional[float] = None
    target_price: Optional[float] = None
    linked_action_key: Optional[str] = None       # a decision of the same result
    linked_decision_id: Optional[str] = None      # an earlier decision (matched-entry baseline)
    linked_round_trip_id: Optional[str] = None    # matched-entry only: the trip whose close this is (Plan 2 R21)
    deployment_digest: Optional[str] = None       # the sized baselines name the deployment a real ENTER uses
    incomplete_reason: Optional[str] = None       # Plan 2 Ruling 18: evidence missing, nothing invented

    def __post_init__(self) -> None:
        if BASELINE_COHORTS.get(self.baseline_id) != self.cohort:
            raise ValueError(f"{self.baseline_id}/{self.cohort} is not an allowed baseline pair")
        if not isinstance(self.opportunity_id, str) or not _OPPORTUNITY_ID.fullmatch(self.opportunity_id):
            raise ValueError("opportunity_id must match ^[A-Za-z0-9_.:-]{1,128}$")
        if not isinstance(self.decided_at, dt.datetime) or self.decided_at.utcoffset() is None:
            raise ValueError("decided_at must be an aware datetime")
        if self.linked_action_key is not None and self.linked_decision_id is not None:
            raise ValueError("a baseline links to one decision at most")
        if self.linked_action_key is not None and not ACTION_KEY.fullmatch(self.linked_action_key):
            raise ValueError("linked_action_key must look like 'enter:265598'")
        if self.linked_decision_id is not None and not _DECISION_ID.fullmatch(self.linked_decision_id):
            raise ValueError("linked_decision_id has a bad shape")
        if self.conid is not None:
            _conid(self.conid)
        if self.side not in (None, "BUY"):
            raise ValueError("baselines are long only (Plan 2 Ruling 6)")
        _positive_int(self.quantity, "quantity")
        for name in ("reference_price", "stop_price", "target_price"):
            _price(getattr(self, name), name)
        self._check_shape()

    def _check_shape(self) -> None:
        """The same shapes Plan 2's RecordSimulatedDecisionRequest enforces, so a bad record never queues."""
        trade = (self.side, self.quantity, self.reference_price, self.stop_price, self.target_price)
        if self.deployment_digest is not None and not _DIGEST.fullmatch(self.deployment_digest):
            raise ValueError("deployment_digest must be sha256:<64 hex>")
        if self.linked_round_trip_id is not None and (
                self.baseline_id != "matched_entry_bracket_exit.v1"
                or not _OPPORTUNITY_ID.fullmatch(self.linked_round_trip_id)):
            raise ValueError("linked_round_trip_id is a matched-entry field with the opportunity id shape")
        if self.baseline_id == "no_trade.v1":
            if any(v is not None for v in (*trade, self.deployment_digest, self.incomplete_reason)):
                raise ValueError("no_trade carries no trade, deployment or incomplete reason")
            return
        if self.conid is None:
            raise ValueError("a trading baseline names its conid")
        if self.incomplete_reason is not None:
            if self.incomplete_reason not in INCOMPLETE_REASONS or any(v is not None for v in trade):
                raise ValueError("an incomplete baseline has a known reason and no side, quantity or prices")
            return
        if any(v is None for v in trade[:1] + trade[2:]):
            raise ValueError("a complete trading baseline needs side and all three prices")
        if self.baseline_id in TRADER_SIZED_BASELINES:
            if self.quantity is not None or self.deployment_digest is None:
                raise ValueError("the trader sizes this baseline: no quantity, and name the deployment")
        elif self.quantity is None:
            raise ValueError("the matched-entry baseline carries the real entry quantity")


@dataclass(frozen=True)
class EngineResult:
    decisions: tuple[ProposedDecision, ...] = ()
    baselines: tuple[SimulatedBaseline, ...] = ()
    note: str = ""                    # short audit text, e.g. "JEV_SKIP"; stored with the opportunity or cycle


class DecisionEngine(Protocol):
    async def on_entry_signal(self, ctx: SignalContext) -> EngineResult: ...

    async def on_exit_signal(self, ctx: SignalContext) -> EngineResult: ...

    async def on_entry_cycle(self, ctx: EntryCycleContext) -> EngineResult: ...

    async def on_position_cycle(self, ctx: PositionCycleContext) -> EngineResult: ...
```

- [ ] **Step 4: Run**

Run: `.venv/bin/python -m pytest tests/ai/runtime/test_engine_contract.py -q --timeout=60`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add trader/ai/ids.py trader/ai/engine.py tests/ai/runtime/scripted_engine.py tests/ai/runtime/test_engine_contract.py
git commit -m "feat: define the ai decision engine contract and code-owned ids

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 6: `Submitter`: persist before send, reconcile unknown outcomes

**Files:**
- Create: `trader/ai/submitter.py`
- Modify (tests): `tests/ai/runtime/fakes.py` (add `FakeLeadership`, `ScriptedTrader`, `receipt`)
- Create (tests): `tests/ai/runtime/test_submitter.py`

**Interfaces:**
- Consumes: `ProposedDecision` (Task 5), `derive_decision_id`, `command_id_for`, `canonical_json`; `RpcNotSent`, `RpcOutcomeUnknown`, `RpcRefused`, `EPOCH_REFUSALS` (Task 2); `Leadership.current_epoch`, `on_stale` (Task 3); `SessionSlots.entry_window_open` (Task 4); Plan 1 `get_ai_paper_decision` reply shape.
- Produces: `SOURCE_KINDS`, `OPEN_STATES`, `UNSETTLED_STATES`, `SEND_MARGIN`; `Submission` (row view); `SubmissionConflict`; `build_body(decision, *, decision_id, expires_at) -> dict`; `Submitter(*, store, supervisor, leadership, clock, slots, experiment_state, not_found_settle_seconds=120)` with `insert_in_tx(conn, *, source_kind, source_id, decision, expires_at, epoch, now) -> str`, `async recover() -> int`, `async send_due()`, `async reconcile_once()`, `async get(decision_id) -> Optional[Submission]`, `async unsettled_count() -> int`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/ai/runtime/fakes.py`:

```python
class FakeLeadership:
    holder_id = "ai-fake00000000"

    def __init__(self, epoch=1):
        self.epoch, self.last_epoch, self.lost = epoch, epoch, []

    def current_epoch(self):
        return self.epoch

    async def on_stale(self, code):
        self.lost.append(code)
        self.epoch = None


def receipt(decision_id, state="SUBMITTED", error_code=None):
    return {"command_id": f"aip-{decision_id}", "correlation_id": f"aip-{decision_id}", "state": state,
            "outcome": None, "error_code": error_code, "retryable": False}


class ScriptedTrader:
    """submit_ai_paper_decision and get_ai_paper_decision. Each submit takes the next scripted step:
    "accept", "accept_lose_reply", ("receipt", state, code) or an exception to raise before anything lands."""

    def __init__(self):
        self.ledger, self.sent, self.reads, self.script = {}, [], [], []
        self.on_submit = None

    async def call(self, method, body, *, epoch=None):
        import json
        from trader.ai.rpc_clients import RpcOutcomeUnknown
        if method == "get_ai_paper_decision":
            self.reads.append((body["decision_id"], epoch))
            found = self.ledger.get(body["decision_id"])
            return {"decision_id": body["decision_id"], "command_id": f"aip-{body['decision_id']}",
                    "found": found is not None, "receipt": found, "decision_state": None,
                    "decision_error_code": None, "close_root_id": None, "controller_epoch": epoch}
        assert method == "submit_ai_paper_decision", method
        self.sent.append((json.dumps(body, sort_keys=True), epoch))
        if self.on_submit is not None:
            self.on_submit(body)
        step = self.script.pop(0) if self.script else "accept"
        if isinstance(step, Exception):
            raise step
        if isinstance(step, tuple):
            self.ledger[body["decision_id"]] = receipt(body["decision_id"], step[1], step[2])
            return self.ledger[body["decision_id"]]
        self.ledger.setdefault(body["decision_id"], receipt(body["decision_id"]))
        if step == "accept_lose_reply":
            raise RpcOutcomeUnknown("REPLY_TIMEOUT")
        return self.ledger[body["decision_id"]]
```

```python
# tests/ai/runtime/test_submitter.py
"""SP2 Plan 5 Task 6: persist the exact command before sending; reconcile unknown outcomes (spec 5.3, 9)."""
import datetime as dt
import json

import pytest

from tests.ai.fakes import FakeClock
from tests.ai.runtime.fakes import AAPL, FakeLeadership, ScriptedTrader, et
from trader.ai.engine import ProposedDecision
from trader.ai.ids import derive_decision_id
from trader.ai.rpc_clients import RpcNotSent, RpcOutcomeUnknown, RpcRefused
from trader.ai.runtime_schema import ALL_MIGRATIONS
from trader.ai.schedule import SessionSlots
from trader.ai.store import AiStore
from trader.ai.submitter import SubmissionConflict, Submitter

SIG = "sig-" + "1" * 32


def enter(**changes):
    base = dict(action_key=f"enter:{AAPL}", action="ENTER", conid=AAPL, side="BUY", decider="jev",
                evidence_digest="sha256:" + "c" * 64, deployment_digest="sha256:" + "a" * 64, policy_revision=1,
                stop_price=225.4, target_price=234.6, quantity=3)
    base.update(changes)
    return ProposedDecision(**base)


def close():
    return ProposedDecision(action_key=f"close:{AAPL}", action="CLOSE", conid=AAPL, side="SELL", decider="orchestrator",
                            evidence_digest="sha256:" + "d" * 64)


class Rig:
    def __init__(self, tmp_path, at=et(11, 0), state="ARMED", epoch=1):
        self.clock = FakeClock(at)
        self.store = AiStore(tmp_path / "ai.duckdb", clock=self.clock)
        self.store.migrate(ALL_MIGRATIONS)
        self.trader, self.leadership, self.state = ScriptedTrader(), FakeLeadership(epoch), state
        self.submitter = self.build()

    def build(self, leadership=None):
        return Submitter(store=self.store, supervisor=self.trader, leadership=leadership or self.leadership,
                         clock=self.clock, slots=SessionSlots(), experiment_state=lambda: self.state)

    async def plan(self, decision=None, ttl=300, source=SIG, kind="entry_signal"):
        now = self.clock.now()
        return await self.store.atransaction(lambda conn: self.submitter.insert_in_tx(
            conn, source_kind=kind, source_id=source, decision=decision or enter(),
            expires_at=now + dt.timedelta(seconds=ttl), epoch=self.leadership.last_epoch, now=now))

    async def row(self, decision_id):
        return await self.submitter.get(decision_id)


@pytest.fixture
def rig(tmp_path):
    return Rig(tmp_path)


@pytest.mark.asyncio
async def test_the_exact_body_and_id_are_persisted_before_the_send(rig):
    decision_id = await rig.plan()
    assert decision_id == derive_decision_id(SIG, f"enter:{AAPL}")
    seen = {}

    def check(body):
        row = rig.store.db.execute("SELECT state, body_json FROM ai_submissions WHERE decision_id = ?",
                                   [decision_id], fetch="one")
        seen.update(state=row[0], stored=json.loads(row[1]), sent=body)
    rig.trader.on_submit = check
    await rig.submitter.send_due()
    assert seen["state"] == "SENDING" and seen["stored"] == seen["sent"]
    assert seen["sent"]["decision_id"] == decision_id and seen["sent"]["expires_at"] == "2026-07-17T15:05:00+00:00"
    row = await rig.row(decision_id)
    assert (row.state, row.receipt_state, row.last_epoch, row.attempts) == ("ACCEPTED", "SUBMITTED", 1, 1)


@pytest.mark.asyncio
async def test_a_proven_presend_failure_stays_unsent_until_it_expires(rig):
    decision_id = await rig.plan(ttl=60)
    rig.trader.script = [RpcNotSent("TRADER_UNREACHABLE")] * 5
    await rig.submitter.send_due()
    row = await rig.row(decision_id)
    assert (row.state, row.error_code, rig.trader.ledger) == ("PENDING", "TRADER_UNREACHABLE", {})
    rig.clock.advance(56)                                       # 4 s left: below the 5 s send margin
    await rig.submitter.send_due()
    assert (await rig.row(decision_id)).state == "ABANDONED"
    assert (await rig.row(decision_id)).error_code == "EXPIRED_UNSENT"


@pytest.mark.asyncio
async def test_a_lost_reply_is_reconciled_by_the_same_id_without_a_resend(rig):         # Review Focus 2
    decision_id = await rig.plan()
    rig.trader.script = ["accept_lose_reply"]
    await rig.submitter.send_due()
    assert (await rig.row(decision_id)).state == "UNKNOWN"
    await rig.submitter.reconcile_once()
    row = await rig.row(decision_id)
    assert (row.state, row.receipt_state) == ("ACCEPTED", "SUBMITTED")
    assert len(rig.trader.sent) == 1 and rig.trader.reads == [(decision_id, 1)]


@pytest.mark.asyncio
async def test_not_found_after_the_settle_window_resends_the_identical_body(rig):
    decision_id = await rig.plan()
    rig.trader.script = [RpcOutcomeUnknown("REPLY_TIMEOUT")]   # the request never landed
    await rig.submitter.send_due()
    await rig.submitter.reconcile_once()
    assert len(rig.trader.sent) == 1                           # inside the settle window: wait
    rig.clock.advance(120)
    await rig.submitter.reconcile_once()
    assert len(rig.trader.sent) == 2 and rig.trader.sent[0][0] == rig.trader.sent[1][0]
    assert (await rig.row(decision_id)).state == "ACCEPTED"


@pytest.mark.asyncio
async def test_not_found_after_expiry_is_not_admitted(rig):
    decision_id = await rig.plan(ttl=60)
    rig.trader.script = [RpcOutcomeUnknown("REPLY_TIMEOUT")]
    await rig.submitter.send_due()
    rig.clock.advance(121)
    await rig.submitter.reconcile_once()
    row = await rig.row(decision_id)
    assert (row.state, row.error_code, len(rig.trader.sent)) == ("NOT_ADMITTED", "NOT_FOUND_AFTER_EXPIRY", 1)


@pytest.mark.asyncio
async def test_an_epoch_refusal_keeps_the_command_pending_and_drops_leadership(rig):    # Review Focus 3
    decision_id = await rig.plan()
    rig.trader.script = [RpcRefused("CONTROLLER_EPOCH_STALE", "stale")]
    await rig.submitter.send_due()
    row = await rig.row(decision_id)
    assert (row.state, rig.leadership.lost, rig.trader.ledger) == ("PENDING", ["CONTROLLER_EPOCH_STALE"], {})
    await rig.submitter.send_due()
    assert len(rig.trader.sent) == 1                            # no epoch, no send
    rig.leadership.epoch = 2                                    # this process takes the next epoch
    await rig.submitter.send_due()
    assert rig.trader.sent[1] == (rig.trader.sent[0][0], 2) and (await rig.row(decision_id)).last_epoch == 2


@pytest.mark.asyncio
async def test_a_rejected_stale_receipt_is_final_and_never_regenerated(rig):
    decision_id = await rig.plan()
    rig.trader.script = [("receipt", "REJECTED", "CONTROLLER_EPOCH_STALE")]
    await rig.submitter.send_due()
    row = await rig.row(decision_id)
    assert (row.state, row.error_code, rig.leadership.lost) == ("FINAL", "CONTROLLER_EPOCH_STALE",
                                                                ["CONTROLLER_EPOCH_STALE"])
    rig.leadership.epoch = 2
    await rig.submitter.send_due()
    await rig.submitter.reconcile_once()
    assert len(rig.trader.sent) == 1


@pytest.mark.asyncio
async def test_a_restart_turns_sending_into_unknown_and_the_successor_reconciles_under_its_epoch(rig):
    decision_id = await rig.plan()
    rig.store.db.execute("UPDATE ai_submissions SET state = 'SENDING', attempts = 1, last_epoch = 1, "
                         "last_sent_at = ? WHERE decision_id = ?", [rig.clock.now(), decision_id])
    rig.trader.ledger[decision_id] = {"command_id": f"aip-{decision_id}", "correlation_id": "c",
                                      "state": "SUBMITTED", "outcome": None, "error_code": None, "retryable": False}
    successor = rig.build(leadership=FakeLeadership(epoch=2))
    assert await successor.recover() == 1
    assert (await successor.get(decision_id)).error_code == "PROCESS_RESTARTED"
    await successor.reconcile_once()
    row = await successor.get(decision_id)
    assert (row.state, row.created_epoch, row.last_epoch) == ("ACCEPTED", 1, 1)
    assert rig.trader.reads == [(decision_id, 2)] and rig.trader.sent == []


@pytest.mark.asyncio
async def test_an_enter_after_the_cutoff_is_abandoned_unsent_but_a_close_is_sent(tmp_path):
    rig = Rig(tmp_path, at=et(15, 31))
    entry_id = await rig.plan()
    close_id = await rig.plan(close(), kind="position_cycle", source="cyc-position-20260717-1530")
    await rig.submitter.send_due()
    assert (await rig.row(entry_id)).error_code == "OUTSIDE_ENTRY_WINDOW"
    assert (await rig.row(close_id)).state == "ACCEPTED"
    assert [json.loads(s)["action"] for s, _ in rig.trader.sent] == ["CLOSE"]


@pytest.mark.asyncio
async def test_an_unarmed_experiment_holds_an_enter_and_a_stopped_one_abandons_a_close(tmp_path):
    rig = Rig(tmp_path, state="PAUSED")
    entry_id = await rig.plan()
    await rig.submitter.send_due()
    assert (await rig.row(entry_id)).state == "PENDING" and rig.trader.sent == []
    rig.state = "STOPPED"
    close_id = await rig.plan(close(), kind="exit_signal")
    await rig.submitter.send_due()
    assert (await rig.row(close_id)).error_code == "EXPERIMENT_STOPPED"


@pytest.mark.asyncio
async def test_nothing_is_sent_without_leadership(tmp_path):
    rig = Rig(tmp_path, epoch=None)
    rig.leadership.last_epoch = 1
    await rig.plan()
    await rig.submitter.send_due()
    await rig.submitter.reconcile_once()
    assert rig.trader.sent == [] and rig.trader.reads == []


@pytest.mark.asyncio
async def test_the_same_id_with_a_different_body_is_a_conflict(rig):
    await rig.plan()
    with pytest.raises(SubmissionConflict):
        await rig.plan(enter(quantity=4))
    assert await rig.plan() == derive_decision_id(SIG, f"enter:{AAPL}")      # the same body is a no-op


@pytest.mark.asyncio
async def test_a_broken_request_fails_loudly(rig):
    decision_id = await rig.plan()
    rig.trader.script = [RpcRefused("VALIDATION_ERROR", "bad")]
    await rig.submitter.send_due()
    assert ((await rig.row(decision_id)).state, (await rig.row(decision_id)).error_code) == ("FAILED",
                                                                                              "VALIDATION_ERROR")
```

- [ ] **Step 2: Run them and see them fail**

Run: `.venv/bin/python -m pytest tests/ai/runtime/test_submitter.py -q --timeout=60`
Expected: `ModuleNotFoundError: No module named 'trader.ai.submitter'`.

- [ ] **Step 3: Implement**

```python
# trader/ai/submitter.py
"""Persist-before-send submission of ai paper decisions (SP2 spec 5.3, 9; Plan 5 Rulings 10-11).

The exact body and id are written before any byte leaves. A proven pre-send
failure keeps the command unsent until it expires. A possible send becomes
UNKNOWN and is reconciled by the same id under the current epoch; a permitted
retry resends the stored bytes, never a regenerated command. No logical
duplicate can exist, because the trader deduplicates by command id and body.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import hashlib
import json
import logging
from dataclasses import dataclass
from typing import Any, Callable, Optional

from trader.ai.engine import ProposedDecision
from trader.ai.ids import canonical_json, command_id_for, derive_decision_id
from trader.ai.rpc_clients import EPOCH_REFUSALS, RpcNotSent, RpcOutcomeUnknown, RpcRefused
from trader.ai.store import to_utc

logger = logging.getLogger(__name__)

SOURCE_KINDS = ("entry_signal", "exit_signal", "entry_cycle", "position_cycle")
OPEN_STATES = ("PENDING", "SENDING", "UNKNOWN", "ACCEPTED")
UNSETTLED_STATES = ("PENDING", "SENDING", "UNKNOWN")
TRADER_FINAL_STATES = frozenset({"RESOLVED", "REJECTED"})
REFUSED_BEFORE_ANY_HANDLER = frozenset({"AUTHENTICATION_ERROR", "REPLAY_ERROR"})
BROKEN_REQUEST = frozenset({"VALIDATION_ERROR", "PERMISSION_DENIED", "METHOD_NOT_ALLOWED"})
SEND_MARGIN = dt.timedelta(seconds=5)
MAX_TTL = dt.timedelta(minutes=15)                 # the trader's MAX_EXPIRY_AHEAD
WAIT = "WAIT"
_COLUMNS = ("decision_id, command_id, source_kind, source_id, action_key, action, body_json, body_sha256, "
            "expires_at, created_epoch, state, attempts, last_epoch, last_sent_at, next_try_at, receipt_state, "
            "close_root_id, error_code")


@dataclass(frozen=True)
class Submission:
    decision_id: str
    command_id: str
    source_kind: str
    source_id: str
    action_key: str
    action: str
    body_json: str
    body_sha256: str
    expires_at: dt.datetime
    created_epoch: int
    state: str
    attempts: int
    last_epoch: Optional[int]
    last_sent_at: Optional[dt.datetime]
    next_try_at: dt.datetime
    receipt_state: Optional[str]
    close_root_id: Optional[str]
    error_code: Optional[str]


def _submission(row: tuple) -> Submission:
    values = list(row)
    for index in (8, 13, 14):
        values[index] = None if values[index] is None else to_utc(values[index])
    return Submission(*values)


class SubmissionConflict(Exception):
    """The same decision id was persisted with a different body: a bug, never resolved silently."""


def build_body(decision: ProposedDecision, *, decision_id: str, expires_at: dt.datetime) -> dict:
    return {"decision_id": decision_id, "deployment_digest": decision.deployment_digest,
            "decider": decision.decider, "action": decision.action, "conid": decision.conid,
            "side": decision.side, "stop_price": decision.stop_price, "target_price": decision.target_price,
            "quantity": decision.quantity, "policy_revision": decision.policy_revision,
            "evidence_digest": decision.evidence_digest,
            "expires_at": expires_at.astimezone(dt.timezone.utc).isoformat()}


class Submitter:
    def __init__(self, *, store: Any, supervisor: Any, leadership: Any, clock: Any, slots: Any,
                 experiment_state: Callable[[], Optional[str]], not_found_settle_seconds: int = 120):
        self._store, self._supervisor, self._leadership = store, supervisor, leadership
        self._clock, self._slots, self._experiment_state = clock, slots, experiment_state
        self._settle = dt.timedelta(seconds=not_found_settle_seconds)
        self._lock = asyncio.Lock()                # one send or reconcile step at a time in this process

    # -- persistence ---------------------------------------------------------------------------------
    def insert_in_tx(self, conn: Any, *, source_kind: str, source_id: str, decision: ProposedDecision,
                     expires_at: dt.datetime, epoch: int, now: dt.datetime) -> str:
        """Persist the exact body and id; the caller's transaction also records why (spec 5.3)."""
        if source_kind not in SOURCE_KINDS:
            raise ValueError(f"unknown source kind {source_kind!r}")
        if type(epoch) is not int or epoch < 1:
            raise ValueError("a decision is persisted under a held epoch")
        if not now < expires_at <= now + MAX_TTL:
            raise ValueError("expires_at must lie within 15 minutes ahead")
        decision_id = derive_decision_id(source_id, decision.action_key)
        body_json = canonical_json(build_body(decision, decision_id=decision_id, expires_at=expires_at))
        digest = hashlib.sha256(body_json.encode()).hexdigest()
        existing = conn.execute("SELECT body_sha256 FROM ai_submissions WHERE decision_id = ?",
                                [decision_id]).fetchone()
        if existing is not None:
            if existing[0] != digest:
                raise SubmissionConflict(decision_id)
            return decision_id
        conn.execute(
            "INSERT INTO ai_submissions (decision_id, command_id, source_kind, source_id, action_key, action, "
            "body_json, body_sha256, expires_at, created_epoch, state, attempts, next_try_at, created_at, "
            "updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'PENDING', 0, ?, ?, ?)",
            [decision_id, command_id_for(decision_id), source_kind, source_id, decision.action_key, decision.action,
             body_json, digest, expires_at, epoch, now, now, now])
        return decision_id

    async def get(self, decision_id: str) -> Optional[Submission]:
        row = await self._store.aquery(f"SELECT {_COLUMNS} FROM ai_submissions WHERE decision_id = ?",
                                       [decision_id], fetch="one")
        return None if row is None else _submission(row)

    async def unsettled_count(self) -> int:
        row = await self._store.aquery(
            "SELECT COUNT(*) FROM ai_submissions WHERE state IN ('PENDING', 'SENDING', 'UNKNOWN')", fetch="one")
        return int(row[0])

    async def _rows(self, where: str, params: list) -> list[Submission]:
        rows = await self._store.aquery(f"SELECT {_COLUMNS} FROM ai_submissions WHERE {where} ORDER BY created_at",
                                        params)
        return [_submission(row) for row in rows]

    async def _set(self, decision_id: str, **fields: Any) -> None:
        fields["updated_at"] = self._clock.now()
        assignments = ", ".join(f"{name} = ?" for name in fields)
        await self._store.atransaction(lambda conn: conn.execute(
            f"UPDATE ai_submissions SET {assignments} WHERE decision_id = ?", [*fields.values(), decision_id]))

    # -- startup ------------------------------------------------------------------------------------
    async def recover(self) -> int:
        """A SENDING row may have reached the trader before the crash: it is UNKNOWN now."""
        now = self._clock.now()

        def work(conn: Any) -> int:
            count = conn.execute("SELECT COUNT(*) FROM ai_submissions WHERE state = 'SENDING'").fetchone()[0]
            conn.execute("UPDATE ai_submissions SET state = 'UNKNOWN', error_code = 'PROCESS_RESTARTED', "
                         "updated_at = ? WHERE state = 'SENDING'", [now])
            return int(count)
        return await self._store.atransaction(work)

    # -- sending --------------------------------------------------------------------------------------
    async def send_due(self) -> None:
        async with self._lock:
            for row in await self._rows("state = 'PENDING' AND next_try_at <= ?", [self._clock.now()]):
                await self._try_send(row, resend=False)

    def _gate(self, row: Submission, now: dt.datetime, *, resend: bool) -> Optional[str]:
        """None to send now, WAIT, or the code that abandons a never-sent command."""
        if now >= row.expires_at - SEND_MARGIN:
            return WAIT if resend else "EXPIRED_UNSENT"
        state = self._experiment_state()
        if row.action == "ENTER":
            if not self._slots.entry_window_open(now):
                return WAIT if resend else "OUTSIDE_ENTRY_WINDOW"
            return None if state == "ARMED" else WAIT
        if state == "STOPPED":
            return WAIT if resend else "EXPERIMENT_STOPPED"
        return None if state is not None else WAIT

    async def _try_send(self, row: Submission, *, resend: bool) -> None:
        now = self._clock.now()
        verdict = self._gate(row, now, resend=resend)
        if verdict == WAIT:
            return
        if verdict is not None:
            await self._set(row.decision_id, state="ABANDONED", error_code=verdict)
            return
        epoch = self._leadership.current_epoch()          # re-checked right before the send (spec 5.3)
        if epoch is None:
            return
        await self._set(row.decision_id, state="SENDING", attempts=row.attempts + 1, last_epoch=epoch,
                        last_sent_at=now)
        not_sent_state = "UNKNOWN" if resend else "PENDING"   # an earlier send of a resend may have landed
        try:
            reply = await self._supervisor.call("submit_ai_paper_decision", json.loads(row.body_json), epoch=epoch)
        except RpcNotSent as exc:
            await self._set(row.decision_id, state=not_sent_state, error_code=exc.code,
                            next_try_at=now + _backoff(row.attempts + 1))
            return
        except RpcOutcomeUnknown as exc:
            await self._set(row.decision_id, state="UNKNOWN", error_code=exc.code)
            return
        except RpcRefused as exc:
            await self._refused(row, exc, not_sent_state, now)
            return
        await self._apply_receipt(row.decision_id, reply, None)

    async def _refused(self, row: Submission, exc: RpcRefused, not_sent_state: str, now: dt.datetime) -> None:
        if exc.code in EPOCH_REFUSALS:                     # Plan 1 Ruling 1a: refused before any ledger row
            await self._set(row.decision_id, state=not_sent_state, error_code=exc.code)
            await self._leadership.on_stale(exc.code)
        elif exc.code in REFUSED_BEFORE_ANY_HANDLER:
            await self._set(row.decision_id, state=not_sent_state, error_code=exc.code,
                            next_try_at=now + _backoff(row.attempts + 1))
        elif exc.code in BROKEN_REQUEST:
            logger.error("ai decision %s refused as a broken request: %s", row.decision_id, exc.code)
            await self._set(row.decision_id, state="UNKNOWN" if not_sent_state == "UNKNOWN" else "FAILED",
                            error_code=exc.code)
        else:                                              # the trader may have started on it
            await self._set(row.decision_id, state="UNKNOWN", error_code=exc.code)

    async def _apply_receipt(self, decision_id: str, receipt: Any, close_root_id: Optional[str]) -> None:
        state = receipt.get("state") if isinstance(receipt, dict) else None
        if not isinstance(state, str):
            logger.error("ai decision %s: receipt without a state; kept UNKNOWN", decision_id)
            await self._set(decision_id, state="UNKNOWN", error_code="RECEIPT_MALFORMED")
            return
        error_code = receipt.get("error_code")
        await self._set(decision_id, state="FINAL" if state in TRADER_FINAL_STATES else "ACCEPTED",
                        receipt_json=canonical_json(receipt), receipt_state=state, error_code=error_code,
                        close_root_id=close_root_id)
        if error_code in EPOCH_REFUSALS:                   # takeover during admission (Plan 1 Ruling 4): lost
            await self._leadership.on_stale(error_code)

    # -- reconciliation -------------------------------------------------------------------------------
    async def reconcile_once(self) -> None:
        """Runs whatever the budget and the model do: it needs only the trader and the epoch."""
        async with self._lock:
            for row in await self._rows("state IN ('UNKNOWN', 'ACCEPTED')", []):
                epoch = self._leadership.current_epoch()
                if epoch is None:
                    return
                try:
                    view = await self._supervisor.call("get_ai_paper_decision", {"decision_id": row.decision_id},
                                                       epoch=epoch)
                except RpcRefused as exc:
                    if exc.code in EPOCH_REFUSALS:
                        await self._leadership.on_stale(exc.code)
                        return
                    logger.error("reconcile read of %s refused: %s", row.decision_id, exc.code)
                    continue
                except (RpcNotSent, RpcOutcomeUnknown):
                    return                                 # trader away: the next pass tries again
                if view.get("found") is True:
                    await self._apply_receipt(row.decision_id, view.get("receipt"), view.get("close_root_id"))
                elif row.state == "UNKNOWN":
                    await self._unknown_not_found(row)
                else:
                    logger.error("accepted ai decision %s has no trader row", row.decision_id)

    async def _unknown_not_found(self, row: Submission) -> None:
        now = self._clock.now()
        if row.last_sent_at is not None and now < row.last_sent_at + self._settle:
            return                                         # a late request may still be handled (30 s skew)
        if now < row.expires_at - SEND_MARGIN:
            await self._try_send(row, resend=True)         # same id, same stored bytes
        else:
            await self._set(row.decision_id, state="NOT_ADMITTED", error_code="NOT_FOUND_AFTER_EXPIRY")


def _backoff(attempts: int) -> dt.timedelta:
    return dt.timedelta(seconds=min(2 ** attempts, 30))
```

- [ ] **Step 4: Run**

Run: `.venv/bin/python -m pytest tests/ai/runtime/test_submitter.py -q --timeout=60`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add trader/ai/submitter.py tests/ai/runtime/fakes.py tests/ai/runtime/test_submitter.py
git commit -m "feat: persist ai decisions before sending and reconcile unknown outcomes

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 7: `ReportingOutbox`: costs and baseline records, delivered once

**Files:**
- Create: `trader/ai/outbox.py`
- Modify (tests): `tests/ai/runtime/fakes.py` (add `FakeIngest`, `write_cost_event`, `write_correction`)
- Create (tests): `tests/ai/runtime/test_outbox.py`

**Interfaces:**
- Consumes: Plan 4 `AttemptJournal.acost_events_after`, `aget`, `CostEvent`, `AttemptRecord`, `COST_CONFIRMED`, `COST_ESTIMATED_UNKNOWN`, `COST_NONE`, `COST_CORRECTION`; Plan 2 `record_ai_cost` / `record_simulated_decision` request and reply shapes (`trader.scoreboard.ingest_models.RecordAiCostRequest`, `RecordSimulatedDecisionRequest` in the tests only); `cost_record_id`, `attempt_ref`, `simulated_record_id`, `canonical_json` (Task 5); `SimulatedBaseline` (Task 5); `set_cursor_in_tx`, `read_cursor` (Task 1).
- Produces: `COST_STATUS`, `COST_CURSOR = "cost_events"`; `micros_to_usd(micros) -> float`; `CallContext(context_key, experiment_id, served_kind, served_id)`; `register_context_in_tx(conn, *, context_key, experiment_id, served_kind, served_id, now)`; `cost_body(event, attempt, context) -> dict`; `simulated_body(experiment_id, baseline) -> dict`; `ReportingOutbox(*, store, journal, supervisor, clock)` with `async pump_costs(limit=100) -> int`, `enqueue_simulated_in_tx(conn, *, experiment_id, baseline, wait_for_decision_id, now) -> str`, `async release_waiting()`, `async deliver_due(limit=50) -> int`, `async counts() -> dict`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/ai/runtime/fakes.py`:

```python
class FakeIngest:
    """Plan 2's ingestion semantics in memory. Script steps: "down", "lose_ack", ("refuse", code, retryable)."""

    def __init__(self):
        self.rows, self.script, self.calls = {}, [], []

    async def call(self, method, body, *, epoch=None):
        from trader.ai.rpc_clients import RpcNotSent, RpcOutcomeUnknown
        self.calls.append((method, body["record_id"]))
        step = self.script.pop(0) if self.script else "ok"
        if step == "down":
            raise RpcNotSent("TRADER_UNREACHABLE")
        if isinstance(step, tuple):
            return {"status": "REFUSED", "record_id": body["record_id"], "code": step[1], "detail": None,
                    "retryable": step[2]}
        known = self.rows.get(body["record_id"])
        if known is not None and known != body:
            return {"status": "REFUSED", "record_id": body["record_id"], "code": "CONFLICTING_DUPLICATE",
                    "detail": None, "retryable": False}
        status = "DUPLICATE" if known is not None else "INSERTED"
        self.rows[body["record_id"]] = body
        if step == "lose_ack":
            raise RpcOutcomeUnknown("REPLY_TIMEOUT")
        return {"status": status, "record_id": body["record_id"], "code": None, "detail": None, "retryable": False}


def write_cost_event(store, *, request_key, kind, cost_micros, now, usage=None):
    """One Plan 4 attempt and its cost event, as the gateway writes them; returns the attempt key."""
    from trader.ai.journal import AttemptJournal
    from trader.ai.model_client import ChatMessage, ModelRequest, ModelResponse
    journal = AttemptJournal(store)

    def work(conn):
        attempt = journal.begin_in_tx(
            conn, request=ModelRequest(request_key=request_key, messages=(ChatMessage("user", "hi"),),
                                       max_output_tokens=10),
            role="jev", backend="openrouter", model="vendor/jev-1", reservation_id=f"res-{request_key}", now=now)
        if kind == "CONFIRMED":
            journal.finish_success_in_tx(conn, attempt.attempt_key,
                                         ModelResponse("ok", usage, "vendor/jev-1", "openrouter", "stop", "g-1"), now)
        else:
            journal.finish_failure_in_tx(conn, attempt.attempt_key,
                                         outcome="NOT_SENT" if kind == "NONE" else "UNKNOWN",
                                         error_code="TEST", error_detail="", now=now)
        journal.add_cost_event_in_tx(conn, attempt=attempt, kind=kind, cost_micros=cost_micros, usage=usage, now=now)
        return attempt.attempt_key
    return store.transaction(work)


def write_correction(store, attempt_key, *, usage, cost_micros, now):
    from trader.ai.journal import AttemptJournal
    journal = AttemptJournal(store)

    def work(conn):
        journal.reconcile_late_usage_in_tx(conn, attempt_key, usage, now)
        journal.add_cost_event_in_tx(conn, attempt=journal.get_in_tx(conn, attempt_key), kind="CORRECTION",
                                     cost_micros=cost_micros, usage=usage, now=now)
    store.transaction(work)
```

```python
# tests/ai/runtime/test_outbox.py
"""SP2 Plan 5 Task 7: costs and baselines reach the trader once (spec 7, 9; Rulings 12-14)."""
import datetime as dt
import json

import pytest

from tests.ai.fakes import FakeClock
from tests.ai.runtime.fakes import AAPL, FakeIngest, FakeLeadership, ScriptedTrader, et, write_correction, \
    write_cost_event
from trader.ai.engine import ProposedDecision, SimulatedBaseline
from trader.ai.ids import attempt_ref, cost_record_id, derive_decision_id
from trader.ai.journal import AttemptJournal
from trader.ai.model_client import Usage
from trader.ai.outbox import ReportingOutbox, micros_to_usd, register_context_in_tx
from trader.ai.runtime_schema import ALL_MIGRATIONS, read_cursor
from trader.ai.schedule import SessionSlots
from trader.ai.store import AiStore
from trader.ai.submitter import Submitter
from trader.scoreboard.ingest_models import RecordAiCostRequest, RecordSimulatedDecisionRequest

EXP = "exp-" + "a" * 20
SIG = "sig-" + "1" * 32


class Rig:
    def __init__(self, tmp_path):
        self.clock = FakeClock(et(11, 0))
        self.store = AiStore(tmp_path / "ai.duckdb", clock=self.clock)
        self.store.migrate(ALL_MIGRATIONS)
        self.ingest = FakeIngest()
        self.outbox = ReportingOutbox(store=self.store, journal=AttemptJournal(self.store), supervisor=self.ingest,
                                      clock=self.clock)
        self.store.transaction(lambda conn: register_context_in_tx(
            conn, context_key=SIG, experiment_id=EXP, served_kind="signal", served_id=SIG, now=self.clock.now()))

    def event(self, kind, cost=80_000, usage=None):
        return write_cost_event(self.store, request_key=f"{SIG}/jev/1", kind=kind, cost_micros=cost,
                                usage=usage, now=self.clock.now())

    def rows(self):
        return self.store.db.execute("SELECT record_id, state, attempts, last_code, delivered_status, source_ref, "
                                     "attempt_key, body_json FROM ai_outbox ORDER BY created_seq", fetch="all")


@pytest.fixture
def rig(tmp_path):
    return Rig(tmp_path)


def test_money_moves_from_micros_to_usd_exactly():
    assert (micros_to_usd(240_000), micros_to_usd(1), micros_to_usd(0)) == (0.24, 0.000001, 0.0)


@pytest.mark.asyncio
@pytest.mark.parametrize("kind,cost,usage,status,usd", [
    ("CONFIRMED", 15_000, Usage(1000, 200), "confirmed", 0.015),
    ("ESTIMATED_UNKNOWN", 80_000, None, "estimated", 0.08),        # the reserved worst case, never confirmed
    ("NONE", 0, None, "confirmed", 0.0)])                           # proven not sent: costs nothing
async def test_each_cost_kind_maps_to_one_valid_record_ai_cost_body(rig, kind, cost, usage, status, usd):
    attempt_key = rig.event(kind, cost, usage)
    assert await rig.outbox.pump_costs() == 1
    record_id, state, _, _, _, source_ref, kept_attempt, body_json = rig.rows()[0]
    body = json.loads(body_json)
    assert (state, source_ref, kept_attempt) == ("PENDING", f"{attempt_key}:{kind}", attempt_key)
    assert record_id == body["record_id"] == cost_record_id(f"{attempt_key}:{kind}")
    assert (body["cost_status"], body["cost_usd"], body["attempt_id"]) == (status, usd, attempt_ref(attempt_key))
    assert (body["experiment_id"], body["served_kind"], body["served_id"], body["decision_id"]) == \
        (EXP, "signal", SIG, None)
    assert body["called_at"] == "2026-07-17T15:00:00+00:00"
    RecordAiCostRequest.model_validate(body)                  # a real Plan 4 event id passes Plan 2's wire model


@pytest.mark.asyncio
async def test_a_correction_points_at_its_estimated_original_and_repeats_its_identity(rig):
    attempt_key = rig.event("ESTIMATED_UNKNOWN")
    rig.clock.advance(600)
    write_correction(rig.store, attempt_key, usage=Usage(900, 100), cost_micros=1_400, now=rig.clock.now())
    await rig.outbox.pump_costs()
    original, correction = (json.loads(row[7]) for row in rig.rows())
    assert correction["corrects_record_id"] == original["record_id"]
    assert (correction["cost_status"], correction["cost_usd"]) == ("confirmed", 0.0014)
    for key in ("role", "provider", "model", "attempt_id", "called_at", "experiment_id"):
        assert correction[key] == original[key]
    RecordAiCostRequest.model_validate(correction)


@pytest.mark.asyncio
async def test_cost_rows_and_the_cursor_commit_together(rig, monkeypatch):
    rig.event("CONFIRMED", 15_000, Usage(10, 5))
    import trader.ai.outbox as outbox_module
    real = outbox_module.set_cursor_in_tx

    def crash(*args, **kwargs):
        raise RuntimeError("process died inside the transaction")
    monkeypatch.setattr(outbox_module, "set_cursor_in_tx", crash)
    with pytest.raises(RuntimeError):
        await rig.outbox.pump_costs()
    assert rig.rows() == [] and await read_cursor(rig.store, "cost_events") == 0
    monkeypatch.setattr(outbox_module, "set_cursor_in_tx", real)
    assert await rig.outbox.pump_costs() == 1 and await rig.outbox.pump_costs() == 0
    assert len(rig.rows()) == 1


@pytest.mark.asyncio
async def test_a_cost_without_its_context_is_dead_and_visible(rig):
    write_cost_event(rig.store, request_key="cyc-entry-20260717-1100/orchestrator/1", kind="NONE",
                     cost_micros=0, now=rig.clock.now())
    await rig.outbox.pump_costs()
    assert rig.rows()[0][1:4] == ("DEAD", 0, "CONTEXT_MISSING")
    assert (await rig.outbox.counts())["dead"] == 1


@pytest.mark.asyncio
async def test_delivery_survives_a_trader_outage(rig):                                 # Review Focus 5
    rig.event("CONFIRMED", 15_000, Usage(10, 5))
    await rig.outbox.pump_costs()
    rig.ingest.script = ["down", "down"]
    assert await rig.outbox.deliver_due() == 0
    rig.clock.advance(5)
    assert await rig.outbox.deliver_due() == 0
    assert rig.rows()[0][1:4] == ("PENDING", 2, "TRADER_UNREACHABLE")
    assert await rig.outbox.deliver_due() == 0                  # backoff: 10 s after the second failure
    rig.clock.advance(10)
    assert await rig.outbox.deliver_due() == 1
    assert rig.rows()[0][1] == "DELIVERED" and len(rig.ingest.rows) == 1


@pytest.mark.asyncio
async def test_a_lost_acknowledgement_never_duplicates(rig):                            # Review Focus 5
    rig.event("CONFIRMED", 15_000, Usage(10, 5))
    await rig.outbox.pump_costs()
    rig.ingest.script = ["lose_ack"]
    await rig.outbox.deliver_due()
    rig.clock.advance(5)
    await rig.outbox.deliver_due()
    assert rig.rows()[0][1] == "DELIVERED" and rig.rows()[0][4] == "DUPLICATE"
    assert len(rig.ingest.rows) == 1 and len(rig.ingest.calls) == 2


@pytest.mark.asyncio
async def test_refusals_follow_the_retryable_flag(rig):
    rig.event("CONFIRMED", 15_000, Usage(10, 5))
    rig.clock.advance(1)
    rig.event("NONE", 0)
    await rig.outbox.pump_costs()
    rig.ingest.script = [("refuse", "CORRECTION_TARGET_UNKNOWN", True), ("refuse", "EXPERIMENT_UNKNOWN", False)]
    await rig.outbox.deliver_due()
    assert [(row[1], row[3]) for row in rig.rows()] == [("PENDING", "CORRECTION_TARGET_UNKNOWN"),
                                                         ("DEAD", "EXPERIMENT_UNKNOWN")]


@pytest.mark.asyncio
async def test_a_baseline_waits_for_its_linked_decision(rig):
    leadership = FakeLeadership(1)
    trader = ScriptedTrader()
    submitter = Submitter(store=rig.store, supervisor=trader, leadership=leadership, clock=rig.clock,
                          slots=SessionSlots(), experiment_state=lambda: "ARMED")
    enter = ProposedDecision(action_key=f"enter:{AAPL}", action="ENTER", conid=AAPL, side="BUY", decider="jev",
                             evidence_digest="sha256:" + "c" * 64, deployment_digest="sha256:" + "a" * 64,
                             policy_revision=1, stop_price=225.4, target_price=234.6, quantity=3)
    follow = SimulatedBaseline("follow_signal.v1", "strategy_signal", SIG, rig.clock.now(), conid=AAPL, side="BUY",
                               reference_price=230.0, stop_price=225.4, target_price=234.6,
                               deployment_digest="sha256:" + "a" * 64, linked_action_key=f"enter:{AAPL}")
    decision_id = derive_decision_id(SIG, f"enter:{AAPL}")
    now = rig.clock.now()

    def commit(conn):
        submitter.insert_in_tx(conn, source_kind="entry_signal", source_id=SIG, decision=enter,
                               expires_at=now + dt.timedelta(minutes=5), epoch=1, now=now)
        rig.outbox.enqueue_simulated_in_tx(conn, experiment_id=EXP, baseline=follow,
                                           wait_for_decision_id=decision_id, now=now)
    await rig.store.atransaction(commit)
    await rig.outbox.deliver_due()
    assert rig.ingest.calls == [] and rig.rows()[0][1] == "WAITING"
    await submitter.send_due()                                       # the trader accepts it
    await rig.outbox.deliver_due()
    body = rig.ingest.rows[rig.rows()[0][0]]
    assert body["linked_decision_id"] == decision_id
    RecordSimulatedDecisionRequest.model_validate(body)


@pytest.mark.asyncio
async def test_a_baseline_of_a_decision_that_never_reached_the_trader_drops_the_link(rig):
    leadership = FakeLeadership(1)
    submitter = Submitter(store=rig.store, supervisor=ScriptedTrader(), leadership=leadership, clock=rig.clock,
                          slots=SessionSlots(), experiment_state=lambda: "ARMED")
    enter = ProposedDecision(action_key=f"enter:{AAPL}", action="ENTER", conid=AAPL, side="BUY", decider="jev",
                             evidence_digest="sha256:" + "c" * 64, quantity=3, stop_price=225.4)
    follow = SimulatedBaseline("follow_signal.v1", "strategy_signal", SIG, rig.clock.now(), conid=AAPL, side="BUY",
                               reference_price=230.0, stop_price=225.4, target_price=234.6,
                               deployment_digest="sha256:" + "a" * 64, linked_action_key=f"enter:{AAPL}")
    now = rig.clock.now()

    def commit(conn):
        submitter.insert_in_tx(conn, source_kind="entry_signal", source_id=SIG, decision=enter,
                               expires_at=now + dt.timedelta(seconds=60), epoch=1, now=now)
        rig.outbox.enqueue_simulated_in_tx(conn, experiment_id=EXP, baseline=follow,
                                           wait_for_decision_id=derive_decision_id(SIG, f"enter:{AAPL}"), now=now)
    await rig.store.atransaction(commit)
    leadership.epoch = None
    rig.clock.advance(58)
    await submitter.send_due()                                       # expired unsent: ABANDONED
    await rig.outbox.deliver_due()
    assert list(rig.ingest.rows.values())[0]["linked_decision_id"] is None
```

- [ ] **Step 2: Run them and see them fail**

Run: `.venv/bin/python -m pytest tests/ai/runtime/test_outbox.py -q --timeout=60`
Expected: `ModuleNotFoundError: No module named 'trader.ai.outbox'`.

- [ ] **Step 3: Implement**

```python
# trader/ai/outbox.py
"""Idempotent delivery of model costs and baseline records to the trader (SP2 spec 7; Plan 5 Rulings 12-14).

Cost events come from Plan 4's journal through a cursor; rows and the cursor
commit together. Every record has a stable id, so a retry after a lost
acknowledgement is a DUPLICATE on the trader, never a second row.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Optional

from trader.ai.engine import SimulatedBaseline
from trader.ai.ids import attempt_ref, canonical_json, cost_record_id, simulated_record_id
from trader.ai.journal import COST_CONFIRMED, COST_CORRECTION, COST_ESTIMATED_UNKNOWN, COST_NONE
from trader.ai.rpc_clients import RpcNotSent, RpcOutcomeUnknown, RpcRefused
from trader.ai.runtime_schema import cursor_value_in_tx, set_cursor_in_tx

logger = logging.getLogger(__name__)

COST_CURSOR = "cost_events"
COST_STATUS = {COST_CONFIRMED: "confirmed", COST_ESTIMATED_UNKNOWN: "estimated", COST_NONE: "confirmed",
               COST_CORRECTION: "confirmed"}
METHOD_BY_KIND = {"cost": "record_ai_cost", "simulated": "record_simulated_decision"}
DELIVERED = frozenset({"INSERTED", "DUPLICATE"})
DEAD_CODES = frozenset({"VALIDATION_ERROR", "PERMISSION_DENIED", "METHOD_NOT_ALLOWED"})
ADMITTED_SUBMISSIONS = ("ACCEPTED", "FINAL")
NEVER_ADMITTED_SUBMISSIONS = ("ABANDONED", "NOT_ADMITTED", "FAILED")
MAX_BACKOFF_SECONDS = 300


def micros_to_usd(micros: int) -> float:
    """Plan 4 money is integer micro-USD; Plan 2 takes a float USD."""
    if type(micros) is not int or micros < 0:
        raise ValueError("cost must be a non-negative integer of micro-USD")
    return float(Decimal(micros) / Decimal(1_000_000))


@dataclass(frozen=True)
class CallContext:
    context_key: str
    experiment_id: str
    served_kind: str
    served_id: str


def register_context_in_tx(conn: Any, *, context_key: str, experiment_id: str, served_kind: str, served_id: str,
                           now: dt.datetime) -> None:
    """Written before any model call of this context, so every cost event can be attributed."""
    conn.execute("INSERT INTO ai_call_contexts VALUES (?, ?, ?, ?, ?) ON CONFLICT (context_key) DO NOTHING",
                 [context_key, experiment_id, served_kind, served_id, now])


def cost_body(event: Any, attempt: Any, context: CallContext) -> dict:
    status = COST_STATUS[event.kind]
    corrects = (cost_record_id(f"{event.attempt_key}:{COST_ESTIMATED_UNKNOWN}")
                if event.kind == COST_CORRECTION else None)
    return {"record_id": cost_record_id(event.event_id), "experiment_id": context.experiment_id,
            "role": event.role, "provider": event.backend, "model": event.model,
            "attempt_id": attempt_ref(event.attempt_key), "input_tokens": event.input_tokens,
            "output_tokens": event.output_tokens, "cost_usd": micros_to_usd(event.cost_micros),
            "cost_status": status, "called_at": attempt.started_at.isoformat(),
            "served_kind": context.served_kind, "served_id": context.served_id, "decision_id": None,
            "corrects_record_id": corrects}


def simulated_body(experiment_id: str, baseline: SimulatedBaseline) -> dict:
    return {"record_id": simulated_record_id(experiment_id, baseline.baseline_id, baseline.opportunity_id),
            "experiment_id": experiment_id, "baseline_id": baseline.baseline_id, "cohort": baseline.cohort,
            "opportunity_id": baseline.opportunity_id, "conid": baseline.conid, "side": baseline.side,
            "quantity": baseline.quantity, "reference_price": baseline.reference_price,
            "stop_price": baseline.stop_price, "target_price": baseline.target_price,
            "decided_at": baseline.decided_at.astimezone(dt.timezone.utc).isoformat(),
            "linked_decision_id": baseline.linked_decision_id,
            "linked_round_trip_id": baseline.linked_round_trip_id,
            "deployment_digest": baseline.deployment_digest, "incomplete_reason": baseline.incomplete_reason}


class ReportingOutbox:
    def __init__(self, *, store: Any, journal: Any, supervisor: Any, clock: Any):
        self._store, self._journal, self._supervisor, self._clock = store, journal, supervisor, clock

    # -- costs ------------------------------------------------------------------------------------------
    async def pump_costs(self, limit: int = 100) -> int:
        cursor = await self._store.atransaction(lambda conn: cursor_value_in_tx(conn, COST_CURSOR))
        # AiStore serializes every write, so event_seq order is commit order and the cursor skips nothing.
        events = await self._journal.acost_events_after(cursor, limit)
        if not events:
            return 0
        prepared = []
        for event in events:
            attempt = await self._journal.aget(event.attempt_key)
            context = None if attempt is None else await self._context(attempt.request_key.split("/", 1)[0])
            prepared.append((event, attempt, context))
        now = self._clock.now()

        def work(conn: Any) -> None:
            for event, attempt, context in prepared:
                if context is None:
                    logger.error("cost event %s has no call context; dead-lettered", event.event_id)
                    body, state, code = {"event_id": event.event_id}, "DEAD", "CONTEXT_MISSING"
                else:
                    body, state, code = cost_body(event, attempt, context), "PENDING", None
                conn.execute(
                    "INSERT INTO ai_outbox (record_id, kind, source_ref, attempt_key, body_json, state, attempts, "
                    "next_try_at, last_code, created_at) VALUES (?, 'cost', ?, ?, ?, ?, 0, ?, ?, ?) "
                    "ON CONFLICT (record_id) DO NOTHING",
                    [cost_record_id(event.event_id), event.event_id, event.attempt_key, canonical_json(body), state,
                     now, code, now])
            set_cursor_in_tx(conn, COST_CURSOR, events[-1].event_seq, now)
        await self._store.atransaction(work)
        return len(events)

    async def _context(self, context_key: str) -> Optional[CallContext]:
        row = await self._store.aquery("SELECT context_key, experiment_id, served_kind, served_id "
                                       "FROM ai_call_contexts WHERE context_key = ?", [context_key], fetch="one")
        return None if row is None else CallContext(*row)

    # -- baselines --------------------------------------------------------------------------------------
    def enqueue_simulated_in_tx(self, conn: Any, *, experiment_id: str, baseline: SimulatedBaseline,
                                wait_for_decision_id: Optional[str], now: dt.datetime) -> str:
        body = simulated_body(experiment_id, baseline)
        conn.execute(
            "INSERT INTO ai_outbox (record_id, kind, source_ref, body_json, state, wait_for_decision_id, attempts, "
            "next_try_at, created_at) VALUES (?, 'simulated', ?, ?, ?, ?, 0, ?, ?) ON CONFLICT (record_id) DO NOTHING",
            [body["record_id"], f"{baseline.baseline_id}|{baseline.opportunity_id}", canonical_json(body),
             "WAITING" if wait_for_decision_id else "PENDING", wait_for_decision_id, now, now])
        return body["record_id"]

    async def release_waiting(self) -> None:
        """A linked baseline goes out once its decision is settled: linked if the trader has it, else null."""
        def work(conn: Any) -> None:
            rows = conn.execute(
                "SELECT o.record_id, o.body_json, o.wait_for_decision_id, s.state FROM ai_outbox o "
                "LEFT JOIN ai_submissions s ON s.decision_id = o.wait_for_decision_id "
                "WHERE o.state = 'WAITING'").fetchall()
            for record_id, body_json, decision_id, submission_state in rows:
                if submission_state in ADMITTED_SUBMISSIONS:
                    link = decision_id
                elif submission_state is None or submission_state in NEVER_ADMITTED_SUBMISSIONS:
                    link = None
                else:
                    continue
                body = json.loads(body_json)
                body["linked_decision_id"] = link
                conn.execute("UPDATE ai_outbox SET body_json = ?, state = 'PENDING' WHERE record_id = ?",
                             [canonical_json(body), record_id])
        await self._store.atransaction(work)

    # -- delivery ---------------------------------------------------------------------------------------
    async def deliver_due(self, limit: int = 50) -> int:
        await self.release_waiting()
        rows = await self._store.aquery(
            "SELECT record_id, kind, body_json, attempts FROM ai_outbox WHERE state = 'PENDING' AND next_try_at <= ? "
            "ORDER BY created_seq LIMIT ?", [self._clock.now(), limit])
        delivered = 0
        for record_id, kind, body_json, attempts in rows:
            try:
                reply = await self._supervisor.call(METHOD_BY_KIND[kind], json.loads(body_json))
            except (RpcNotSent, RpcOutcomeUnknown) as exc:
                await self._retry(record_id, attempts, exc.code)
                break                                  # the trader is away: keep the order, try later
            except RpcRefused as exc:
                await (self._dead(record_id, exc.code) if exc.code in DEAD_CODES
                       else self._retry(record_id, attempts, exc.code))
                continue
            status = reply.get("status") if isinstance(reply, dict) else None
            if status in DELIVERED:
                await self._mark(record_id, state="DELIVERED", delivered_status=status,
                                 delivered_at=self._clock.now())
                delivered += 1
            elif status == "REFUSED" and reply.get("retryable") is True:
                await self._retry(record_id, attempts, reply.get("code") or "REFUSED")
            else:
                await self._dead(record_id, (reply or {}).get("code") or "REPLY_MALFORMED")
        return delivered

    async def counts(self) -> dict:
        rows = await self._store.aquery("SELECT state, COUNT(*) FROM ai_outbox GROUP BY state")
        found = {state: int(count) for state, count in rows}
        return {"waiting": found.get("WAITING", 0), "pending": found.get("PENDING", 0),
                "delivered": found.get("DELIVERED", 0), "dead": found.get("DEAD", 0)}

    async def _retry(self, record_id: str, attempts: int, code: str) -> None:
        delay = min(5 * 2 ** attempts, MAX_BACKOFF_SECONDS)
        await self._mark(record_id, attempts=attempts + 1, last_code=code,
                         next_try_at=self._clock.now() + dt.timedelta(seconds=delay))

    async def _dead(self, record_id: str, code: str) -> None:
        logger.error("outbox record %s dead-lettered: %s", record_id, code)
        await self._mark(record_id, state="DEAD", last_code=code)

    async def _mark(self, record_id: str, **fields: Any) -> None:
        assignments = ", ".join(f"{name} = ?" for name in fields)
        await self._store.atransaction(lambda conn: conn.execute(
            f"UPDATE ai_outbox SET {assignments} WHERE record_id = ?", [*fields.values(), record_id]))
```

- [ ] **Step 4: Run**

Run: `.venv/bin/python -m pytest tests/ai/runtime/test_outbox.py -q --timeout=60`
Expected: all pass. If Plan 4's `finish_failure_in_tx` names the outcomes as module constants, use those constants in `write_cost_event`; the strings above are their values.

- [ ] **Step 5: Commit**

```bash
git add trader/ai/outbox.py tests/ai/runtime/fakes.py tests/ai/runtime/test_outbox.py
git commit -m "feat: deliver ai costs and baseline records through an idempotent outbox

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 8: `SignalIntake`: opportunities and the cursor in one transaction

**Files:**
- Create: `trader/ai/signal_intake.py`
- Modify (tests): `tests/ai/runtime/fakes.py` (add `FakeSignals`, `signal`)
- Create (tests): `tests/ai/runtime/test_signal_intake.py`

**Interfaces:**
- Consumes: Plan 1 `read_ai_signals` (`{after_cursor, limit}` → `{signals, next_cursor, oldest_retained_cursor, gap}`, `SIGNAL_CURSOR_AHEAD`, `source_event_id` `^sig-[0-9a-f]{32}$`); `SignalOpportunity`, `parse_aware` (Task 5); `set_cursor_in_tx`, `cursor_value_in_tx` (Task 1).
- Produces: `SIGNAL_CURSOR = "signals"`; `SignalIntakeError(code, detail)`; `SignalIntake(*, store, supervisor, clock, page_limit=100, max_age_seconds=300)` with `async poll() -> list[str]`, `is_fresh(opportunity, now) -> bool`, `async expire_stale() -> list[str]`, `async open_opportunities() -> list[tuple[SignalOpportunity, str]]`, `async mark(opportunity_id, state, reason)`, `finish_in_tx(conn, opportunity_id, ok, reason)`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/ai/runtime/fakes.py`:

```python
def signal(cursor, *, action="BUY", conid=AAPL, at=None, strategy="orb"):
    import hashlib
    at = at or et(11, 0)
    return {"cursor": cursor, "source_event_id": "sig-" + hashlib.sha256(f"{strategy}{cursor}".encode()).hexdigest()[:32],
            "strategy_name": strategy, "conid": conid, "action": action, "probability": 0.6,
            "signal_time": at.isoformat(), "recorded_at": at.isoformat()}


class FakeSignals:
    """Plan 1's read_ai_signals: a cursor-ordered record with a retention watermark and resets."""

    def __init__(self):
        self.record, self.watermark, self.calls = [], 0, []

    def add(self, **kwargs):
        self.record.append(signal(len(self.record) + 1, **kwargs))
        return self.record[-1]

    async def call(self, method, body, *, epoch=None):
        from trader.ai.rpc_clients import RpcRefused
        assert method == "read_ai_signals"
        self.calls.append((body["after_cursor"], epoch))
        last = self.record[-1]["cursor"] if self.record else self.watermark
        if body["after_cursor"] > last:
            raise RpcRefused("SIGNAL_CURSOR_AHEAD", "the record was reset")
        start = max(body["after_cursor"], self.watermark)
        rows = [s for s in self.record if s["cursor"] > start][:body["limit"]]
        return {"signals": rows, "next_cursor": rows[-1]["cursor"] if rows else start,
                "oldest_retained_cursor": self.watermark + 1, "gap": body["after_cursor"] < self.watermark}
```

```python
# tests/ai/runtime/test_signal_intake.py
"""SP2 Plan 5 Task 8: durable signal intake (spec 5.5, amendment 6.1, Ruling 9)."""
import pytest

from tests.ai.fakes import FakeClock
from tests.ai.runtime.fakes import FakeSignals, et
from trader.ai.runtime_schema import ALL_MIGRATIONS, read_cursor, set_cursor_in_tx
from trader.ai.signal_intake import SignalIntake, SignalIntakeError
from trader.ai.store import AiStore


class Rig:
    def __init__(self, tmp_path):
        self.clock = FakeClock(et(11, 0, 30))
        self.store = AiStore(tmp_path / "ai.duckdb", clock=self.clock)
        self.store.migrate(ALL_MIGRATIONS)
        self.signals = FakeSignals()
        self.intake = SignalIntake(store=self.store, supervisor=self.signals, clock=self.clock, page_limit=2)

    def opportunities(self):
        return self.store.db.execute("SELECT opportunity_id, state, reason FROM ai_opportunities "
                                     "ORDER BY signal_cursor", fetch="all")

    def gaps(self):
        return self.store.db.execute("SELECT kind, after_cursor, resumed_cursor FROM ai_coverage_gaps", fetch="all")


@pytest.fixture
def rig(tmp_path):
    return Rig(tmp_path)


@pytest.mark.asyncio
async def test_new_signals_become_opportunities_and_move_the_cursor(rig):
    added = [rig.signals.add() for _ in range(3)]
    assert await rig.intake.poll() == [s["source_event_id"] for s in added[:2]]     # one page of two
    assert await read_cursor(rig.store, "signals") == 2
    assert await rig.intake.poll() == [added[2]["source_event_id"]]
    assert [row[1] for row in rig.opportunities()] == ["NEW"] * 3


@pytest.mark.asyncio
async def test_crash_between_intake_and_cursor_skips_no_signal(rig, monkeypatch):    # Review Focus 4
    added = [rig.signals.add() for _ in range(2)]
    import trader.ai.signal_intake as intake_module

    def crash(*args, **kwargs):
        raise RuntimeError("process died before the cursor moved")
    monkeypatch.setattr(intake_module, "set_cursor_in_tx", crash)
    with pytest.raises(RuntimeError):
        await rig.intake.poll()
    assert rig.opportunities() == [] and await read_cursor(rig.store, "signals") == 0
    monkeypatch.setattr(intake_module, "set_cursor_in_tx", set_cursor_in_tx)
    assert await rig.intake.poll() == [s["source_event_id"] for s in added]


@pytest.mark.asyncio
async def test_a_redelivered_signal_is_not_a_new_opportunity(rig):                  # Review Focus 4
    rig.signals.add()
    await rig.intake.poll()
    await rig.store.atransaction(lambda conn: set_cursor_in_tx(conn, "signals", 0, rig.clock.now()))
    assert await rig.intake.poll() == []
    assert len(rig.opportunities()) == 1


@pytest.mark.asyncio
async def test_a_retention_gap_is_recorded_not_reconstructed(rig):
    for _ in range(5):
        rig.signals.add()
    rig.signals.watermark = 3                                                 # cursors 1-3 were pruned unread
    new = await rig.intake.poll()
    assert len(new) == 2 and rig.gaps() == [("RETENTION", 0, 4)]
    assert len(rig.opportunities()) == 2                                      # only what was actually seen


@pytest.mark.asyncio
async def test_cursor_ahead_records_a_gap_and_restarts_from_zero(rig):
    await rig.store.atransaction(lambda conn: set_cursor_in_tx(conn, "signals", 40, rig.clock.now()))
    rig.signals.add()
    assert await rig.intake.poll() == []
    assert rig.gaps() == [("CURSOR_AHEAD", 40, 0)] and await read_cursor(rig.store, "signals") == 0
    assert len(await rig.intake.poll()) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("field,value", [("source_event_id", "sig-xyz"), ("action", "NEUTRAL"), ("conid", True),
                                         ("signal_time", "2026-07-17T15:00:00"), ("cursor", "2")])
async def test_a_malformed_signal_fails_loudly_and_commits_nothing(rig, field, value):
    rig.signals.add()
    rig.signals.add()[field] = value
    with pytest.raises(SignalIntakeError):
        await rig.intake.poll()
    assert rig.opportunities() == [] and await read_cursor(rig.store, "signals") == 0


@pytest.mark.asyncio
async def test_retained_stale_signals_become_missed(rig):
    old = rig.signals.add(at=et(10, 50))
    fresh = rig.signals.add(at=et(11, 0))
    await rig.intake.poll()
    assert await rig.intake.expire_stale() == [old["source_event_id"]]
    assert [(row[0], row[1], row[2]) for row in rig.opportunities()] == [
        (old["source_event_id"], "MISSED", "STALE"), (fresh["source_event_id"], "NEW", None)]


@pytest.mark.asyncio
async def test_the_read_carries_the_held_epoch_from_the_client(rig):
    rig.signals.add()
    await rig.intake.poll()
    assert rig.signals.calls == [(0, None)]          # the epoch is attached by PrincipalClient, not by the intake
```

- [ ] **Step 2: Run them and see them fail**

Run: `.venv/bin/python -m pytest tests/ai/runtime/test_signal_intake.py -q --timeout=60`
Expected: `ModuleNotFoundError: No module named 'trader.ai.signal_intake'`.

- [ ] **Step 3: Implement**

```python
# trader/ai/signal_intake.py
"""Durable intake of the trader's strategy signal record (SP2 spec 5.5, amendment 6.1; Plan 5 Ruling 9).

New opportunities and the consumer cursor commit in one transaction, so a crash
never skips a signal; the source_event_id makes a redelivery a no-op. A pruned
range or a reset record is written down as a coverage gap. The missing signals
are never reconstructed or claimed as identified.
"""
from __future__ import annotations

import datetime as dt
import logging
import math
import re
from typing import Any

from trader.ai.engine import SignalOpportunity, parse_aware
from trader.ai.rpc_clients import RpcRefused
from trader.ai.runtime_schema import cursor_value_in_tx, set_cursor_in_tx
from trader.ai.store import to_utc

logger = logging.getLogger(__name__)

SIGNAL_CURSOR = "signals"
SOURCE_EVENT_ID = re.compile(r"^sig-[0-9a-f]{32}$")
_COLUMNS = ("opportunity_id, signal_cursor, strategy_name, conid, action, probability, signal_time, recorded_at, "
            "state")


class SignalIntakeError(Exception):
    def __init__(self, code: str, detail: str = ""):
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code


def _nonnegative_int(value: Any, name: str) -> int:
    if type(value) is not int or value < 0:
        raise SignalIntakeError("PAGE_MALFORMED", f"{name} must be a non-negative integer")
    return value


def _parse_signal(raw: Any) -> SignalOpportunity:
    if not isinstance(raw, dict):
        raise SignalIntakeError("SIGNAL_MALFORMED", "a signal must be an object")
    source = raw.get("source_event_id")
    if not isinstance(source, str) or not SOURCE_EVENT_ID.fullmatch(source):
        raise SignalIntakeError("SIGNAL_MALFORMED", "source_event_id")
    if raw.get("action") not in ("BUY", "SELL"):
        raise SignalIntakeError("SIGNAL_MALFORMED", f"{source}: action")
    conid, probability = raw.get("conid"), raw.get("probability")
    if type(conid) is not int or conid <= 0:
        raise SignalIntakeError("SIGNAL_MALFORMED", f"{source}: conid")
    if probability is not None and (type(probability) is not float or not math.isfinite(probability)):
        raise SignalIntakeError("SIGNAL_MALFORMED", f"{source}: probability")
    if not isinstance(raw.get("strategy_name"), str) or not raw["strategy_name"]:
        raise SignalIntakeError("SIGNAL_MALFORMED", f"{source}: strategy_name")
    try:
        signal_time = parse_aware(raw.get("signal_time"), "signal_time")
        recorded_at = parse_aware(raw.get("recorded_at"), "recorded_at")
    except ValueError as exc:
        raise SignalIntakeError("SIGNAL_MALFORMED", f"{source}: {exc}") from None
    return SignalOpportunity(source, _nonnegative_int(raw.get("cursor"), "cursor"), raw["strategy_name"], conid,
                             raw["action"], probability, signal_time, recorded_at)


def _opportunity(row: tuple) -> tuple[SignalOpportunity, str]:
    values = list(row)
    return SignalOpportunity(values[0], int(values[1]), values[2], int(values[3]), values[4], values[5],
                             to_utc(values[6]), to_utc(values[7])), values[8]


class SignalIntake:
    def __init__(self, *, store: Any, supervisor: Any, clock: Any, page_limit: int = 100, max_age_seconds: int = 300):
        self._store, self._supervisor, self._clock = store, supervisor, clock
        self._limit = page_limit
        self._max_age = dt.timedelta(seconds=max_age_seconds)

    async def poll(self) -> list[str]:
        """One page from the trader. Returns the ids of new opportunities, in cursor order."""
        cursor = await self._store.atransaction(lambda conn: cursor_value_in_tx(conn, SIGNAL_CURSOR))
        try:
            page = await self._supervisor.call("read_ai_signals", {"after_cursor": cursor, "limit": self._limit})
        except RpcRefused as exc:
            if exc.code != "SIGNAL_CURSOR_AHEAD":
                raise
            await self._record_reset(cursor)
            return []
        if not isinstance(page, dict) or type(page.get("gap")) is not bool or not isinstance(page.get("signals"), list):
            raise SignalIntakeError("PAGE_MALFORMED", "read_ai_signals reply has the wrong shape")
        signals = [_parse_signal(raw) for raw in page["signals"]]        # any bad signal: nothing is committed
        next_cursor = _nonnegative_int(page.get("next_cursor"), "next_cursor")
        oldest = _nonnegative_int(page.get("oldest_retained_cursor"), "oldest_retained_cursor")
        if next_cursor < cursor:
            raise SignalIntakeError("PAGE_MALFORMED", "next_cursor moved backwards")
        now = self._clock.now()

        def work(conn: Any) -> list[str]:
            if page["gap"]:
                conn.execute("INSERT INTO ai_coverage_gaps VALUES (?, 'RETENTION', ?, ?, ?) "
                             "ON CONFLICT (gap_id) DO NOTHING", [f"gap-r-{cursor}-{oldest}", cursor, oldest, now])
            new = []
            for s in signals:
                if conn.execute("SELECT 1 FROM ai_opportunities WHERE opportunity_id = ?",
                                [s.opportunity_id]).fetchone():
                    continue                                              # redelivered: not a new opportunity
                conn.execute("INSERT INTO ai_opportunities VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'NEW', NULL, ?, ?)",
                             [s.opportunity_id, s.signal_cursor, s.strategy_name, s.conid, s.action, s.probability,
                              s.signal_time, s.recorded_at, now, now])
                new.append(s.opportunity_id)
            set_cursor_in_tx(conn, SIGNAL_CURSOR, next_cursor, now)
            return new
        new = await self._store.atransaction(work)
        if page["gap"]:
            logger.warning("signal coverage gap: signals after cursor %s and before %s were removed by retention",
                           cursor, oldest)
        return new

    async def _record_reset(self, cursor: int) -> None:
        now = self._clock.now()
        logger.error("the trader's signal record is behind cursor %s (reset); restarting from 0", cursor)

        def work(conn: Any) -> None:
            conn.execute("INSERT INTO ai_coverage_gaps VALUES (?, 'CURSOR_AHEAD', ?, 0, ?) "
                         "ON CONFLICT (gap_id) DO NOTHING", [f"gap-a-{cursor}-{now:%Y%m%dT%H%M%S}", cursor, now])
            set_cursor_in_tx(conn, SIGNAL_CURSOR, 0, now)
        await self._store.atransaction(work)

    def is_fresh(self, opportunity: SignalOpportunity, now: dt.datetime) -> bool:
        return now - opportunity.signal_time <= self._max_age

    async def expire_stale(self) -> list[str]:
        now = self._clock.now()
        stale = [opp.opportunity_id for opp, state in await self.open_opportunities()
                 if state == "NEW" and not self.is_fresh(opp, now)]
        for opportunity_id in stale:
            await self.mark(opportunity_id, "MISSED", "STALE")
        return stale

    async def open_opportunities(self) -> list[tuple[SignalOpportunity, str]]:
        rows = await self._store.aquery(f"SELECT {_COLUMNS} FROM ai_opportunities "
                                        "WHERE state IN ('NEW', 'IN_PROGRESS') ORDER BY signal_cursor")
        return [_opportunity(row) for row in rows]

    async def mark(self, opportunity_id: str, state: str, reason: Any) -> None:
        now = self._clock.now()
        await self._store.atransaction(lambda conn: conn.execute(
            "UPDATE ai_opportunities SET state = ?, reason = ?, updated_at = ? WHERE opportunity_id = ?",
            [state, reason, now, opportunity_id]))

    def finish_in_tx(self, conn: Any, opportunity_id: str, ok: bool, reason: Any) -> None:
        conn.execute("UPDATE ai_opportunities SET state = ?, reason = ?, updated_at = ? WHERE opportunity_id = ?",
                     ["DECIDED" if ok else "FAILED", reason, self._clock.now(), opportunity_id])
```

- [ ] **Step 4: Run**

Run: `.venv/bin/python -m pytest tests/ai/runtime/test_signal_intake.py -q --timeout=60`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add trader/ai/signal_intake.py tests/ai/runtime/fakes.py tests/ai/runtime/test_signal_intake.py
git commit -m "feat: take strategy signals into ai opportunities with an atomic cursor

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 9: `AiController`: loops, dispatch, cycles and atomic commit of engine results

**Files:**
- Create: `trader/ai/controller.py`, `trader/ai/budget_cap.py`
- Modify (tests): `tests/ai/runtime/fakes.py` (add `EXP_ID`, `FakeTrader`, `open_trip`, `FakeGateway`)
- Create (tests): `tests/ai/runtime/test_controller.py`, `tests/ai/runtime/test_budget_cap.py`

**Interfaces:**
- Consumes: Tasks 1–8; Plan 4 `ModelCaller.new_deadline`, `Budget.set_cap`, `Budget.snapshot`, `window_date`, `usd_to_micros_floor`, `CallRefused`; Plan 2 `get_ai_model_budget`.
- Produces: `BudgetCapSync(*, supervisor, budget, clock)` with `async sync() -> bool`, `ready() -> bool`, `last_error: Optional[str]`; `CapGatedGateway(gateway, cap)` (a `ModelCaller`: `new_deadline` passes through, `call` refuses `BUDGET_CAP_UNKNOWN` while `not cap.ready()`); `ExperimentWatch(supervisor)` (`view`, `known`, `async refresh()`, `state() -> Optional[str]`); `validate_result(source_kind, result) -> Optional[str]`; `AiController(*, config, store, clock, supervisor, leadership, watch, submitter, outbox, intake, slots, engine, gateway, cap_sync=None)` with `async start()`, `async refresh_experiment()`, `async tick_signals()`, `async dispatch_opportunities()`, `async run_due_slots()`, `async record_cycle(slot, state, reason)`, `async run_cycle(slot, positions=())`, `async reconcile_once()`, `async report_once()`, `async heartbeat() -> dict`, `async drain()`, `async run(stop)`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/ai/runtime/fakes.py`:

```python
EXP_ID = "exp-" + "b" * 20


def open_trip(conid=AAPL, quantity=3.0, decision_id="dec-" + "9" * 32):
    return {"round_trip_id": f"rt-{conid}", "conid": conid, "symbol": "AAPL", "direction": "LONG",
            "opened_at": et(10, 0).isoformat(), "closed_at": None, "opened_quantity": quantity,
            "closed_quantity": None, "exec_ids": [], "net_pnl_usd": None, "decision_id": decision_id,
            "strategy_ref": "orb", "state": "OPEN"}


class FakeTrader:
    """One trader for the controller tests: experiment view, trips, signals, decisions and ingestion."""

    def __init__(self):
        self.signals, self.decisions, self.ingest = FakeSignals(), ScriptedTrader(), FakeIngest()
        self.state, self.entry_block, self.trips, self.experiment_down = "ARMED", None, [], False

    async def call(self, method, body, *, epoch=None):
        from trader.ai.rpc_clients import RpcNotSent
        if method == "get_experiment":
            if self.experiment_down:
                raise RpcNotSent("TRADER_UNREACHABLE")
            if self.state is None:
                return {"experiment": None, "entry_block": None}
            return {"experiment": {"experiment_id": EXP_ID, "state": self.state, "started_at": et(9, 31).isoformat()},
                    "entry_block": self.entry_block}
        if method == "get_experiment_trips":
            return {"experiment_id": body["experiment_id"], "generation": 1, "trips": list(self.trips)}
        if method == "read_ai_signals":
            return await self.signals.call(method, body, epoch=epoch)
        if method in ("record_ai_cost", "record_simulated_decision"):
            return await self.ingest.call(method, body, epoch=epoch)
        return await self.decisions.call(method, body, epoch=epoch)


class FakeGateway:
    """Only deadlines: the scripted engine never calls a model."""

    def __init__(self, clock):
        self.clock = clock

    def new_deadline(self, label=""):
        from trader.ai.gateway import DecisionDeadline
        return DecisionDeadline(self.clock, 60, label)

    async def call(self, role, request, deadline):
        raise AssertionError("no model call in the Plan 5 tests")
```

```python
# tests/ai/runtime/test_controller.py
"""SP2 Plan 5 Task 9: the controller schedules, dispatches and commits; the engine only judges (spec 5.2, 5.5)."""
import asyncio
import datetime as dt
import json

import pytest
import pytest_asyncio

from tests.ai.fakes import FakeClock
from tests.ai.runtime.fakes import AAPL, FRIDAY, MSFT, FakeGateway, FakeLeadership, FakeTrader, et, open_trip
from tests.ai.runtime.scripted_engine import ScriptedEngine
from trader.ai.config import ControllerConfig
from trader.ai.controller import AiController, ExperimentWatch
from trader.ai.engine import EngineResult, ProposedDecision, SimulatedBaseline
from trader.ai.ids import derive_decision_id
from trader.ai.journal import AttemptJournal
from trader.ai.outbox import ReportingOutbox
from trader.ai.runtime_schema import ALL_MIGRATIONS, set_cursor_in_tx
from trader.ai.schedule import Slot, SessionSlots
from trader.ai.signal_intake import SignalIntake
from trader.ai.store import AiStore
from trader.ai.submitter import Submitter


def enter(conid=AAPL):
    return ProposedDecision(action_key=f"enter:{conid}", action="ENTER", conid=conid, side="BUY", decider="jev",
                            evidence_digest="sha256:" + "c" * 64, deployment_digest="sha256:" + "a" * 64,
                            policy_revision=1, stop_price=225.4, target_price=234.6, quantity=3)


def close(conid=AAPL):
    return ProposedDecision(action_key=f"close:{conid}", action="CLOSE", conid=conid, side="SELL",
                            decider="orchestrator", evidence_digest="sha256:" + "d" * 64)


class Rig:
    def __init__(self, tmp_path, at):
        self.tmp_path, self.clock = tmp_path, FakeClock(at)
        self.store = AiStore(tmp_path / "ai.duckdb", clock=self.clock)
        self.store.migrate(ALL_MIGRATIONS)
        self.trader, self.leadership, self.engine = FakeTrader(), FakeLeadership(1), ScriptedEngine()
        self.build()

    def build(self):
        self.watch = ExperimentWatch(self.trader)
        slots = SessionSlots()
        self.submitter = Submitter(store=self.store, supervisor=self.trader, leadership=self.leadership,
                                   clock=self.clock, slots=slots, experiment_state=self.watch.state)
        self.intake = SignalIntake(store=self.store, supervisor=self.trader, clock=self.clock)
        self.controller = AiController(
            config=ControllerConfig(heartbeat_path=str(self.tmp_path / "hb.json")), store=self.store,
            clock=self.clock, supervisor=self.trader, leadership=self.leadership, watch=self.watch,
            submitter=self.submitter, outbox=ReportingOutbox(store=self.store, journal=AttemptJournal(self.store),
                                                             supervisor=self.trader, clock=self.clock),
            intake=self.intake, slots=slots, engine=self.engine, gateway=FakeGateway(self.clock))

    def sent(self):
        return [json.loads(body) for body, _ in self.trader.decisions.sent]

    def cycles(self):
        return self.store.db.execute("SELECT cycle_id, state, reason FROM ai_cycles ORDER BY cycle_id", fetch="all")

    def opportunity(self, signal):
        return self.store.db.execute("SELECT state, reason FROM ai_opportunities WHERE opportunity_id = ?",
                                     [signal["source_event_id"]], fetch="one")

    async def signals_then_drain(self):
        await self.controller.tick_signals()
        await self.controller.drain()

    async def slots_then_drain(self):
        await self.controller.run_due_slots()
        await self.controller.drain()


async def rig_at(tmp_path, at, **trader):
    rig = Rig(tmp_path, at)
    for name, value in trader.items():
        setattr(rig.trader, name, value)
    await rig.controller.start()
    return rig


@pytest_asyncio.fixture
async def rig(tmp_path):
    return await rig_at(tmp_path, et(11, 0, 30))


@pytest.mark.asyncio
async def test_an_entry_signal_submits_one_enter_and_records_its_baseline(rig):
    s = rig.trader.signals.add()
    rig.engine.results["entry_signal"] = lambda ctx: EngineResult(decisions=(enter(),), baselines=(
        SimulatedBaseline("follow_signal.v1", "strategy_signal", ctx.opportunity.opportunity_id, ctx.now,
                          conid=AAPL, side="BUY", reference_price=230.0, stop_price=225.4, target_price=234.6,
                          deployment_digest="sha256:" + "a" * 64, linked_action_key=f"enter:{AAPL}"),))
    await rig.signals_then_drain()
    decision_id = derive_decision_id(s["source_event_id"], f"enter:{AAPL}")
    assert [b["decision_id"] for b in rig.sent()] == [decision_id]
    assert rig.opportunity(s) == ("DECIDED", None)
    await rig.controller.report_once()
    (baseline,) = rig.trader.ingest.rows.values()
    assert baseline["linked_decision_id"] == decision_id and baseline["opportunity_id"] == s["source_event_id"]


@pytest.mark.asyncio
async def test_an_exit_signal_goes_to_the_exit_hook_only(rig):
    rig.trader.signals.add(action="SELL")
    rig.engine.results["exit_signal"] = EngineResult(decisions=(close(),))
    await rig.signals_then_drain()
    assert rig.engine.hooks_called() == ["exit_signal"] and [b["action"] for b in rig.sent()] == ["CLOSE"]


@pytest.mark.asyncio
async def test_a_redelivered_signal_is_judged_once(rig):
    rig.trader.signals.add()
    await rig.signals_then_drain()
    await rig.store.atransaction(lambda conn: set_cursor_in_tx(conn, "signals", 0, rig.clock.now()))
    await rig.signals_then_drain()
    assert rig.engine.hooks_called() == ["entry_signal"]


@pytest.mark.asyncio
async def test_a_multi_action_cycle_gets_one_derived_id_per_action(rig):
    rig.engine.results["entry_cycle"] = EngineResult(decisions=(enter(AAPL), enter(MSFT)))
    await rig.slots_then_drain()
    cycle = "cyc-entry-20260717-1100"
    assert sorted(b["decision_id"] for b in rig.sent()) == sorted(
        [derive_decision_id(cycle, f"enter:{AAPL}"), derive_decision_id(cycle, f"enter:{MSFT}")])
    assert rig.cycles() == [(cycle, "DONE", None), ("cyc-position-20260717-1100", "SKIPPED", "NO_OWNED_POSITIONS")]


@pytest.mark.asyncio
async def test_an_engine_cannot_enter_from_a_position_cycle(tmp_path):
    rig = await rig_at(tmp_path, et(11, 0, 30), trips=[open_trip()])
    rig.engine.results["position_cycle"] = EngineResult(decisions=(enter(),))
    await rig.slots_then_drain()
    assert ("cyc-position-20260717-1100", "FAILED", "ACTION_NOT_ALLOWED_HERE") in rig.cycles()
    assert rig.sent() == []                                   # the entry cycle of the same slot proposed nothing


@pytest.mark.asyncio
async def test_a_position_cycle_closes_after_the_entry_cutoff(tmp_path):
    rig = await rig_at(tmp_path, et(15, 30, 10), trips=[open_trip()])
    rig.engine.results["position_cycle"] = EngineResult(decisions=(close(),))
    await rig.slots_then_drain()
    assert rig.engine.hooks_called() == ["position_cycle"]
    assert [b["action"] for b in rig.sent()] == ["CLOSE"]
    assert rig.cycles() == [("cyc-entry-20260717-1515", "MISSED", "LATE_START"),
                            ("cyc-position-20260717-1530", "DONE", None)]
    (_, ctx), = rig.engine.calls
    assert [p.conid for p in ctx.positions] == [AAPL]


@pytest.mark.asyncio
async def test_position_cycles_run_while_paused_and_skip_without_positions(tmp_path):
    rig = await rig_at(tmp_path, et(11, 0, 30), state="PAUSED")
    await rig.slots_then_drain()
    assert rig.cycles() == [("cyc-entry-20260717-1100", "SKIPPED", "EXPERIMENT_PAUSED"),
                            ("cyc-position-20260717-1100", "SKIPPED", "NO_OWNED_POSITIONS")]
    rig.trader.trips = [open_trip()]
    rig.clock.advance(15 * 60)
    await rig.slots_then_drain()
    assert rig.engine.hooks_called() == ["position_cycle"]


@pytest.mark.asyncio
async def test_killed_runs_no_cycles_but_an_exit_signal_still_closes(tmp_path):
    rig = await rig_at(tmp_path, et(11, 0, 30), state="KILLED", trips=[open_trip()])
    buy, sell = rig.trader.signals.add(), rig.trader.signals.add(action="SELL")
    rig.engine.results["exit_signal"] = EngineResult(decisions=(close(),))
    await rig.slots_then_drain()
    await rig.signals_then_drain()
    assert {state for _, state, _ in rig.cycles()} == {"SKIPPED"}
    assert rig.opportunity(buy) == ("MISSED", "EXPERIMENT_KILLED") and rig.opportunity(sell) == ("DECIDED", None)
    assert [b["action"] for b in rig.sent()] == ["CLOSE"]                  # the trader joins it to the kill flatten


@pytest.mark.asyncio
async def test_stopped_admits_nothing_new(tmp_path):
    rig = await rig_at(tmp_path, et(11, 0, 30), state="STOPPED", trips=[open_trip()])
    buy, sell = rig.trader.signals.add(), rig.trader.signals.add(action="SELL")
    await rig.slots_then_drain()
    await rig.signals_then_drain()
    assert rig.engine.calls == [] and rig.sent() == []
    assert rig.opportunity(buy) == ("MISSED", "EXPERIMENT_STOPPED") == rig.opportunity(sell)


@pytest.mark.asyncio
async def test_a_missed_slot_is_recorded_and_never_replayed(tmp_path):
    rig = await rig_at(tmp_path, et(11, 3))
    await rig.slots_then_drain()
    assert rig.cycles()[0] == ("cyc-entry-20260717-1100", "MISSED", "LATE_START")
    rig.clock.advance(12 * 60 + 5)                                          # 11:15:05
    await rig.slots_then_drain()
    assert [ctx.slot.cycle_id for hook, ctx in rig.engine.calls if hook == "entry_cycle"] == \
        ["cyc-entry-20260717-1115"]


@pytest.mark.asyncio
async def test_slow_model_work_never_blocks_receipts_or_reconciliation(rig):
    now = rig.clock.now()
    decision_id = await rig.store.atransaction(lambda conn: rig.submitter.insert_in_tx(
        conn, source_kind="entry_signal", source_id="sig-" + "2" * 32, decision=enter(),
        expires_at=now + dt.timedelta(minutes=5), epoch=1, now=now))
    rig.store.db.execute("UPDATE ai_submissions SET state = 'UNKNOWN', last_sent_at = ? WHERE decision_id = ?",
                         [now, decision_id])
    rig.trader.decisions.ledger[decision_id] = {"command_id": f"aip-{decision_id}", "correlation_id": "c",
                                                "state": "SUBMITTED", "outcome": None, "error_code": None,
                                                "retryable": False}
    rig.engine.block = asyncio.Event()
    await rig.controller.run_due_slots()                                    # the entry cycle now waits on the model
    await asyncio.wait_for(rig.controller.reconcile_once(), timeout=2)
    await asyncio.wait_for(rig.controller.tick_signals(), timeout=2)
    assert (await rig.submitter.get(decision_id)).state == "ACCEPTED"
    rig.engine.block.set()
    await rig.controller.drain()


@pytest.mark.asyncio
async def test_a_cycle_is_cut_at_its_slot_deadline(rig):
    rig.engine.block = asyncio.Event()
    rig.engine.results["entry_cycle"] = EngineResult(decisions=(enter(),))
    slot = Slot("entry", FRIDAY, et(11, 0), rig.clock.now() + dt.timedelta(seconds=0.05))
    await rig.controller.record_cycle(slot, "RUNNING", None)
    await rig.controller.run_cycle(slot)
    assert rig.cycles() == [("cyc-entry-20260717-1100", "TIMED_OUT", "SLOT_DEADLINE")] and rig.sent() == []


@pytest.mark.asyncio
async def test_an_unfinished_opportunity_is_judged_again_if_fresh_else_missed(rig):
    fresh, stale = rig.trader.signals.add(at=et(10, 58)), rig.trader.signals.add(at=et(10, 50))
    await rig.intake.poll()
    for s in (fresh, stale):
        await rig.intake.mark(s["source_event_id"], "IN_PROGRESS", None)          # the process died while judging
    rig.build()
    await rig.controller.start()
    await rig.controller.dispatch_opportunities()
    await rig.controller.drain()
    assert rig.opportunity(fresh) == ("DECIDED", None) and rig.opportunity(stale) == ("MISSED", "STALE")


@pytest.mark.asyncio
async def test_a_buy_outside_the_entry_window_is_missed(tmp_path):
    rig = await rig_at(tmp_path, et(15, 31))
    buy = rig.trader.signals.add(at=et(15, 31))
    await rig.signals_then_drain()
    assert rig.opportunity(buy) == ("MISSED", "OUTSIDE_ENTRY_WINDOW") and rig.engine.calls == []


@pytest.mark.asyncio
async def test_the_cost_context_exists_before_the_hook_runs(rig):
    seen = []

    def check(ctx):
        seen.append(rig.store.db.execute("SELECT experiment_id, served_kind, served_id FROM ai_call_contexts "
                                         "WHERE context_key = ?", [ctx.work.context_key], fetch="one"))
        return EngineResult()
    s = rig.trader.signals.add()
    rig.engine.results["entry_signal"] = check
    await rig.signals_then_drain()
    assert seen == [(rig.watch.view.experiment_id, "signal", s["source_event_id"])]


@pytest.mark.asyncio
async def test_an_unknown_experiment_is_never_treated_as_no_experiment(rig):
    rig.trader.experiment_down = True
    await rig.controller.refresh_experiment()
    buy = rig.trader.signals.add()
    await rig.signals_then_drain()
    assert rig.opportunity(buy) == ("NEW", None) and rig.engine.calls == []


@pytest.mark.asyncio
async def test_an_engine_error_fails_only_that_opportunity(rig):
    rig.engine.error = RuntimeError("bug")
    buy = rig.trader.signals.add()
    await rig.signals_then_drain()
    assert rig.opportunity(buy) == ("FAILED", "ENGINE_ERROR") and rig.sent() == []


@pytest.mark.asyncio
async def test_the_heartbeat_reports_leadership_and_open_work(rig, tmp_path):
    status = await rig.controller.heartbeat()
    written = json.loads((tmp_path / "hb.json").read_text())
    assert written == status and (status["epoch"], status["unsettled_submissions"]) == (1, 0)
```

- [ ] **Step 2: Run them and see them fail**

Run: `.venv/bin/python -m pytest tests/ai/runtime/test_controller.py -q --timeout=60`
Expected: `ModuleNotFoundError: No module named 'trader.ai.controller'`.

- [ ] **Step 3: Implement**

```python
# trader/ai/controller.py
"""The ai controller: one async service with a durable journal (SP2 spec 3, 4, 5.2, 5.5, 9; Plan 5 Rulings 7-9, 15).

Deterministic code only: schedule, intake, submission, reconciliation and
reporting. Trading judgments come from the DecisionEngine. Each loop is its own
task and model work runs in spawned tasks, so a slow model call never blocks
receipts, reconciliation or recovery.
"""
from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
import json
import logging
import os
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

from trader.ai.engine import (
    ALLOWED_ACTIONS, EngineResult, EntryCycleContext, ExperimentView, ModelWork, PositionCycleContext,
    SignalContext, SignalOpportunity, owned_positions_from_trips,
)
from trader.ai.ids import derive_decision_id
from trader.ai.outbox import register_context_in_tx
from trader.ai.rpc_clients import RpcNotSent, RpcOutcomeUnknown, RpcRefused
from trader.ai.schedule import ENTRY, POSITION, Slot

logger = logging.getLogger(__name__)

WAIT = "WAIT"
SLOT_POLL_SECONDS = 1.0
CYCLE_SOURCE = {ENTRY: "entry_cycle", POSITION: "position_cycle"}
TRADER_AWAY = (RpcNotSent, RpcOutcomeUnknown, RpcRefused)


class ExperimentWatch:
    """The trader's experiment as last read. Unknown (trader away, bad reply) is never 'no experiment'."""

    def __init__(self, supervisor: Any):
        self._supervisor = supervisor
        self.view: Optional[ExperimentView] = None
        self.known = False

    async def refresh(self) -> None:
        try:
            reply = await self._supervisor.call("get_experiment", {})
            self.view = ExperimentView.from_reply(reply)
        except TRADER_AWAY as exc:
            self.known = False
            logger.warning("experiment view unavailable: %s", exc.code)
            return
        except ValueError as exc:
            self.known = False
            logger.error("experiment view malformed: %s", exc)
            return
        self.known = True

    def state(self) -> Optional[str]:
        return self.view.state if self.known and self.view is not None else None


def validate_result(source_kind: str, result: Any) -> Optional[str]:
    """Ruling 15: the whole result is refused if one part does not fit its source."""
    if not isinstance(result, EngineResult):
        return "RESULT_TYPE"
    keys = [decision.action_key for decision in result.decisions]
    if len(keys) != len(set(keys)):
        return "DUPLICATE_ACTION_KEY"
    if any(decision.action not in ALLOWED_ACTIONS[source_kind] for decision in result.decisions):
        return "ACTION_NOT_ALLOWED_HERE"
    if any(b.linked_action_key is not None and b.linked_action_key not in keys for b in result.baselines):
        return "BASELINE_LINK_UNKNOWN"
    return None


def _write_atomically(path: str, text: str) -> None:
    temporary = Path(path).with_suffix(".tmp")
    temporary.write_text(text)
    os.replace(temporary, path)


class AiController:
    def __init__(self, *, config: Any, store: Any, clock: Any, supervisor: Any, leadership: Any,
                 watch: ExperimentWatch, submitter: Any, outbox: Any, intake: Any, slots: Any, engine: Any,
                 gateway: Any, cap_sync: Any = None):
        self._config, self._store, self._clock = config, store, clock
        self._supervisor, self._leadership, self._watch = supervisor, leadership, watch
        self._submitter, self._outbox, self._intake = submitter, outbox, intake
        self._slots, self._engine, self._gateway = slots, engine, gateway
        self._cap_sync = cap_sync                      # BudgetCapSync (Ruling 19); None in unit tests
        self._ttl = dt.timedelta(seconds=config.decision_ttl_seconds)
        self._tasks: set[asyncio.Task] = set()
        self._opportunity_tasks: dict[str, asyncio.Task] = {}
        self._cycle_tasks: dict[str, asyncio.Task] = {}

    # -- lifecycle ---------------------------------------------------------------------------------
    def _spawn(self, coroutine: Awaitable[None]) -> asyncio.Task:
        task = asyncio.create_task(coroutine)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    async def drain(self) -> None:
        while self._tasks:
            await asyncio.gather(*list(self._tasks), return_exceptions=True)

    async def start(self) -> None:
        """After a restart: possibly-sent work is reconciled, unfinished cycles are never replayed (spec 9)."""
        await self._submitter.recover()
        now = self._clock.now()
        await self._store.atransaction(lambda conn: conn.execute(
            "UPDATE ai_cycles SET state = 'FAILED', reason = 'PROCESS_RESTARTED', finished_at = ? "
            "WHERE state = 'RUNNING'", [now]))
        await self._watch.refresh()
        if self._cap_sync is not None:
            await self._cap_sync.sync()                # a failure is logged; the gateway stays closed (Ruling 19)

    async def refresh_experiment(self) -> None:
        await self._watch.refresh()

    # -- model work identity -------------------------------------------------------------------------
    async def _open_work(self, source_id: str, served_kind: str, experiment: ExperimentView) -> ModelWork:
        async def register(context_key: str, kind: str, served_id: str) -> None:
            now = self._clock.now()
            await self._store.atransaction(lambda conn: register_context_in_tx(
                conn, context_key=context_key, experiment_id=experiment.experiment_id, served_kind=kind,
                served_id=served_id, now=now))
        await register(source_id, served_kind, source_id)
        return ModelWork(context_key=source_id, served_kind=served_kind, served_id=source_id, source_id=source_id,
                         experiment_id=experiment.experiment_id, gateway=self._gateway,
                         deadline=self._gateway.new_deadline(source_id), register=register)

    async def _commit(self, source_kind: str, source_id: str, experiment: ExperimentView, result: Any,
                      finish: Callable[[Any, bool, Optional[str]], None]) -> None:
        """Decisions, baselines and the opportunity or cycle state in one transaction."""
        problem = validate_result(source_kind, result)
        if problem is not None:
            logger.error("engine result for %s refused: %s", source_id, problem)
            await self._store.atransaction(lambda conn: finish(conn, False, problem))
            return
        epoch = self._leadership.last_epoch
        if epoch is None:
            raise RuntimeError("model work ran without ever holding a controller epoch")
        now = self._clock.now()
        expires_at = now + self._ttl

        def work(conn: Any) -> None:
            for decision in result.decisions:
                self._submitter.insert_in_tx(conn, source_kind=source_kind, source_id=source_id, decision=decision,
                                             expires_at=expires_at, epoch=epoch, now=now)
            for baseline in result.baselines:
                wait_for = (None if baseline.linked_action_key is None
                            else derive_decision_id(source_id, baseline.linked_action_key))
                self._outbox.enqueue_simulated_in_tx(conn, experiment_id=experiment.experiment_id,
                                                     baseline=baseline, wait_for_decision_id=wait_for, now=now)
            finish(conn, True, result.note or None)
        await self._store.atransaction(work)

    # -- signals -------------------------------------------------------------------------------------
    async def tick_signals(self) -> None:
        if self._leadership.current_epoch() is None:
            return
        await self._intake.poll()
        await self._intake.expire_stale()
        await self.dispatch_opportunities()

    async def dispatch_opportunities(self) -> None:
        now = self._clock.now()
        for opportunity, _state in await self._intake.open_opportunities():
            if opportunity.opportunity_id in self._opportunity_tasks:
                continue
            verdict = self._signal_verdict(opportunity, now)
            if verdict == WAIT:
                continue
            if verdict is not None:
                await self._intake.mark(opportunity.opportunity_id, "MISSED", verdict)
                continue
            await self._intake.mark(opportunity.opportunity_id, "IN_PROGRESS", None)
            task = self._spawn(self._judge(opportunity))
            self._opportunity_tasks[opportunity.opportunity_id] = task
            task.add_done_callback(lambda _t, key=opportunity.opportunity_id: self._opportunity_tasks.pop(key, None))

    def _signal_verdict(self, opportunity: SignalOpportunity, now: dt.datetime) -> Optional[str]:
        if not self._intake.is_fresh(opportunity, now):
            return "STALE"
        if self._leadership.current_epoch() is None or not self._watch.known:
            return WAIT
        experiment = self._watch.view
        if experiment is None:
            return "NO_EXPERIMENT"
        if opportunity.action == "BUY":
            if experiment.state != "ARMED":
                return f"EXPERIMENT_{experiment.state}"
            if experiment.entry_block:
                return "ENTRY_BLOCKED"
            if not self._slots.entry_window_open(now):
                return "OUTSIDE_ENTRY_WINDOW"
            return None
        return "EXPERIMENT_STOPPED" if experiment.state == "STOPPED" else None

    async def _judge(self, opportunity: SignalOpportunity) -> None:
        experiment = self._watch.view
        buy = opportunity.action == "BUY"
        hook = self._engine.on_entry_signal if buy else self._engine.on_exit_signal

        def finish(conn: Any, ok: bool, reason: Optional[str]) -> None:
            self._intake.finish_in_tx(conn, opportunity.opportunity_id, ok, reason)
        try:
            work = await self._open_work(opportunity.opportunity_id, "signal", experiment)
            result = await hook(SignalContext(self._clock.now(), experiment, opportunity, work))
            await self._commit("entry_signal" if buy else "exit_signal", opportunity.opportunity_id, experiment,
                               result, finish)
        except asyncio.CancelledError:
            raise                                          # IN_PROGRESS stays: judged again after a restart if fresh
        except Exception:
            logger.exception("judging opportunity %s failed", opportunity.opportunity_id)
            await self._intake.mark(opportunity.opportunity_id, "FAILED", "ENGINE_ERROR")
            return
        await self._submitter.send_due()

    # -- slots ---------------------------------------------------------------------------------------
    async def run_due_slots(self) -> None:
        now = self._clock.now()
        for kind in (POSITION, ENTRY):
            slot = self._slots.latest(kind, now)
            if slot is None or await self._cycle_recorded(slot.cycle_id):
                continue
            if not self._slots.is_due(slot, now):
                await self.record_cycle(slot, "MISSED", "LATE_START")
                continue
            running = self._cycle_tasks.get(kind)
            if running is not None and not running.done():
                await self.record_cycle(slot, "MISSED", "PREVIOUS_CYCLE_RUNNING")
                continue
            if self._leadership.current_epoch() is None or not self._watch.known:
                continue                                   # decide later, while the slot is still due
            skip, positions = await self._cycle_gate(kind)
            if skip == WAIT:
                continue
            if skip is not None:
                await self.record_cycle(slot, "SKIPPED", skip)
                continue
            await self.record_cycle(slot, "RUNNING", None)
            self._cycle_tasks[kind] = self._spawn(self.run_cycle(slot, positions))

    async def _cycle_gate(self, kind: str) -> tuple[Optional[str], tuple]:
        experiment = self._watch.view
        if experiment is None:
            return "NO_EXPERIMENT", ()
        if kind == ENTRY:
            if experiment.state != "ARMED":
                return f"EXPERIMENT_{experiment.state}", ()
            return ("ENTRY_BLOCKED", ()) if experiment.entry_block else (None, ())
        if experiment.state not in ("ARMED", "PAUSED"):
            return f"EXPERIMENT_{experiment.state}", ()
        try:
            positions = owned_positions_from_trips(await self._supervisor.call(
                "get_experiment_trips", {"experiment_id": experiment.experiment_id}))
        except (*TRADER_AWAY, ValueError) as exc:
            logger.warning("owned positions unavailable (%s); the position slot waits", exc)
            return WAIT, ()
        return (None, positions) if positions else ("NO_OWNED_POSITIONS", ())

    async def _cycle_recorded(self, cycle_id: str) -> bool:
        return await self._store.aquery("SELECT 1 FROM ai_cycles WHERE cycle_id = ?", [cycle_id],
                                        fetch="one") is not None

    async def record_cycle(self, slot: Slot, state: str, reason: Optional[str]) -> None:
        now = self._clock.now()
        finished = None if state == "RUNNING" else now
        await self._store.atransaction(lambda conn: conn.execute(
            "INSERT INTO ai_cycles VALUES (?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT (cycle_id) DO NOTHING",
            [slot.cycle_id, slot.kind, f"{slot.session_date:%Y-%m-%d}", slot.start, state, reason, now, finished]))

    def _finish_cycle_in_tx(self, conn: Any, cycle_id: str, state: str, reason: Optional[str]) -> None:
        conn.execute("UPDATE ai_cycles SET state = ?, reason = ?, finished_at = ? WHERE cycle_id = ?",
                     [state, reason, self._clock.now(), cycle_id])

    async def run_cycle(self, slot: Slot, positions: tuple = ()) -> None:
        experiment = self._watch.view

        def finish(conn: Any, ok: bool, reason: Optional[str]) -> None:
            self._finish_cycle_in_tx(conn, slot.cycle_id, "DONE" if ok else "FAILED", reason)

        async def finish_now(state: str, reason: str) -> None:
            await self._store.atransaction(lambda conn: self._finish_cycle_in_tx(conn, slot.cycle_id, state, reason))
        try:
            work = await self._open_work(slot.cycle_id, "cycle", experiment)
            now = self._clock.now()
            if slot.kind == ENTRY:
                pending = self._engine.on_entry_cycle(EntryCycleContext(now, experiment, slot, work))
            else:
                pending = self._engine.on_position_cycle(PositionCycleContext(now, experiment, slot, positions, work))
            budget = max((slot.deadline - now).total_seconds(), 0.0)
            result = await asyncio.wait_for(pending, timeout=budget)       # bounded work per slot (spec 5.2)
            await self._commit(CYCLE_SOURCE[slot.kind], slot.cycle_id, experiment, result, finish)
        except TimeoutError:
            await finish_now("TIMED_OUT", "SLOT_DEADLINE")
            return
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("cycle %s failed", slot.cycle_id)
            await finish_now("FAILED", "ENGINE_ERROR")
            return
        await self._submitter.send_due()

    # -- receipts, reporting, status ----------------------------------------------------------------
    async def reconcile_once(self) -> None:
        """Independent of cycles and of the budget (spec 5.2)."""
        await self._submitter.reconcile_once()
        await self._submitter.send_due()

    async def report_once(self) -> None:
        await self._outbox.pump_costs()
        await self._outbox.deliver_due()

    async def heartbeat(self) -> dict:
        status = {"at": self._clock.now().isoformat(), "holder_id": self._leadership.holder_id,
                  "epoch": self._leadership.current_epoch(),
                  "unsettled_submissions": await self._submitter.unsettled_count(),
                  "outbox": await self._outbox.counts(),
                  "running_cycles": sorted(kind for kind, task in self._cycle_tasks.items() if not task.done()),
                  "budget_cap_ready": None if self._cap_sync is None else self._cap_sync.ready()}
        if self._config.heartbeat_path:
            await asyncio.to_thread(_write_atomically, self._config.heartbeat_path, json.dumps(status))
        return status

    async def _every(self, stop: asyncio.Event, seconds: float, step: Callable[[], Awaitable[Any]],
                     name: str) -> None:
        while not stop.is_set():
            try:
                await step()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("ai controller step %s failed", name)
            await self._clock.sleep(seconds)

    async def run(self, stop: asyncio.Event) -> None:
        await self.start()
        cfg = self._config
        loops = [asyncio.create_task(self._leadership.run_renewals(stop))]
        for seconds, step, name in ((cfg.experiment_poll_seconds, self.refresh_experiment, "experiment"),
                                    (cfg.signal_poll_seconds, self.tick_signals, "signals"),
                                    (SLOT_POLL_SECONDS, self.run_due_slots, "slots"),
                                    (cfg.reconcile_seconds, self.reconcile_once, "reconcile"),
                                    (cfg.outbox_seconds, self.report_once, "outbox"),
                                    (cfg.heartbeat_seconds, self.heartbeat, "heartbeat")):
            loops.append(asyncio.create_task(self._every(stop, seconds, step, name)))
        if self._cap_sync is not None:
            loops.append(asyncio.create_task(self._every(stop, cfg.budget_cap_poll_seconds, self._cap_sync.sync,
                                                         "budget_cap")))
        try:
            await stop.wait()
        finally:
            for task in [*loops, *self._tasks]:
                task.cancel()
            await asyncio.gather(*loops, *list(self._tasks), return_exceptions=True)
            with contextlib.suppress(Exception):
                await self.heartbeat()
```

**The owner's cap (Ruling 19).** `trader/ai/budget_cap.py`:

```python
"""The owner's daily model cap, read from the trader (trader.yaml), never from ai.yaml (SP2 Plan 5 Ruling 19)."""
from __future__ import annotations

import logging
import math
from typing import Any, Optional

from trader.ai.budget import window_date
from trader.ai.config import usd_to_micros_floor
from trader.ai.gateway import CallRefused

logger = logging.getLogger(__name__)
CAP_METHOD = "get_ai_model_budget"
CAP_SOURCE = "trader.yaml"


def parse_cap_reply(reply: Any) -> float:
    if not isinstance(reply, dict) or set(reply) != {"model_budget_usd_per_day", "source"}:
        raise ValueError("CAP_REPLY_SHAPE")
    value = reply["model_budget_usd_per_day"]
    if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
        raise ValueError("CAP_REPLY_VALUE")
    if reply["source"] != CAP_SOURCE:
        raise ValueError("CAP_REPLY_SOURCE")
    return float(value)


class BudgetCapSync:
    def __init__(self, *, supervisor: Any, budget: Any, clock: Any):
        self._supervisor, self._budget, self._clock = supervisor, budget, clock
        self._synced_window: Optional[str] = None
        self.last_error: Optional[str] = "NOT_READ_YET"

    async def sync(self) -> bool:
        """One read and one set_cap. Any failure closes the gate until a later read succeeds."""
        try:
            value = parse_cap_reply(await self._supervisor.call(CAP_METHOD, {}))
            outcome = await self._budget.set_cap(usd_to_micros_floor(value))
        except Exception as exc:              # RpcNotSent, RpcOutcomeUnknown, RpcRefused, a bad reply
            self._synced_window, self.last_error = None, getattr(exc, "code", None) or str(exc) or type(exc).__name__
            logger.error("owner budget cap not read (%s): no new model calls until it is", self.last_error)
            return False
        self._synced_window, self.last_error = window_date(self._clock.now()), None
        logger.info("owner budget cap %.2f USD/day applied: %s", value, outcome)
        return True

    def ready(self) -> bool:
        return self._synced_window is not None and self._synced_window == window_date(self._clock.now())


class CapGatedGateway:
    """The ModelCaller the engine and the controller use: no model call without a current owner cap."""

    def __init__(self, gateway: Any, cap: BudgetCapSync):
        self._gateway, self._cap = gateway, cap
        self.budget, self.journal = gateway.budget, gateway.journal

    def new_deadline(self, label: str = ""):
        return self._gateway.new_deadline(label)

    async def call(self, role, request, deadline):
        if not self._cap.ready():
            raise CallRefused("BUDGET_CAP_UNKNOWN", self._cap.last_error or "the cap is from another window")
        return await self._gateway.call(role, request, deadline)
```

`AiController` (code above) reads the cap in `start()`, polls it in `run()` and reports `budget_cap_ready` in the heartbeat.

`tests/ai/runtime/test_budget_cap.py` uses Plan 4's `AiStore`, `Budget`, `FakeClock`, `World`, `request`, `config_text`, `write_config`, `load_ai_config`, `AiConfigError`, and these local helpers: `reply(usd) = {"model_budget_usd_per_day": usd, "source": "trader.yaml"}`; `CapTrader(answer)` whose `async call(method, body)` asserts `method == "get_ai_model_budget"` and returns `answer` (or raises it when it is an exception), with `set(answer)`; `migrated_store(tmp_path, clock)` = an `AiStore` on `tmp_path / "ai.duckdb"` after `migrate(ALL_MIGRATIONS)`; `et` from `fakes` (Friday 2026-07-17):

```python
USD = 1_000_000


@pytest.mark.asyncio
async def test_the_cap_comes_from_the_trader_and_a_raise_waits_for_new_york_midnight(tmp_path):
    clock, trader = FakeClock(et(11, 0)), CapTrader(reply(1.0))
    store = migrated_store(tmp_path, clock)
    sync = BudgetCapSync(supervisor=trader, budget=Budget(store, clock, calls_per_hour=120), clock=clock)
    assert await sync.sync() and sync.ready()
    trader.set(reply(5.0))                                         # the operator edited trader.yaml and restarted it
    assert await sync.sync()
    snapshot = await Budget(store, clock, calls_per_hour=120).snapshot()
    assert (snapshot.effective_cap_micros, snapshot.pending_cap_micros) == (1 * USD, 5 * USD)
    clock.advance(13 * 3600 + 60)                                 # 00:01 New York, the next day
    assert not sync.ready()                                        # a new New York window needs a new read
    assert await sync.sync() and (await Budget(store, clock, calls_per_hour=120).snapshot()).effective_cap_micros == 5 * USD


@pytest.mark.asyncio
async def test_an_ai_restart_or_ai_config_change_cannot_raise_the_cap_early(tmp_path):
    clock, trader = FakeClock(et(11, 0)), CapTrader(reply(1.0))
    store = migrated_store(tmp_path, clock)
    await BudgetCapSync(supervisor=trader, budget=Budget(store, clock, calls_per_hour=120), clock=clock).sync()
    trader.set(reply(5.0))
    await BudgetCapSync(supervisor=trader, budget=Budget(store, clock, calls_per_hour=120), clock=clock).sync()
    # a new ai process on the same ai.duckdb, with an ai.yaml that tries to name a cap
    with pytest.raises(AiConfigError):
        load_ai_config(str(write_config(tmp_path, config_text(extra_top_level="model_budget_usd_per_day: 9999"))))
    restarted = BudgetCapSync(supervisor=trader, budget=Budget(store, clock, calls_per_hour=120), clock=clock)
    assert await restarted.sync()
    assert (await Budget(store, clock, calls_per_hour=120).snapshot()).effective_cap_micros == 1 * USD


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", [RpcNotSent("NO_ROUTE"), {"model_budget_usd_per_day": True, "source": "trader.yaml"},
                                 {"model_budget_usd_per_day": -1, "source": "trader.yaml"},
                                 {"model_budget_usd_per_day": float("nan"), "source": "trader.yaml"},
                                 {"model_budget_usd_per_day": 5, "source": "ai.yaml"}, {"x": 1}])
async def test_a_failed_cap_read_stops_model_calls_until_a_read_succeeds(tmp_path, bad):
    clock, trader = FakeClock(et(11, 0)), CapTrader(reply(2000.0))
    world = World(tmp_path, clock)                                 # Plan 4's gateway world
    await world.gateway.start()
    sync = BudgetCapSync(supervisor=trader, budget=world.gateway.budget, clock=clock)
    gated = CapGatedGateway(world.gateway, sync)
    assert await sync.sync()
    trader.set(bad)
    assert await sync.sync() is False and not sync.ready()
    with pytest.raises(CallRefused) as caught:
        await gated.call("jev", request("d/jev/1"), gated.new_deadline())
    assert caught.value.code == "BUDGET_CAP_UNKNOWN"
    assert world.rows("SELECT count(*) FROM ai_budget_reservations") == [(0,)] and world.jev.requests == []
    trader.set(reply(2000.0))
    assert await sync.sync() and (await gated.call("jev", request("d/jev/1"), gated.new_deadline())).response.text
```

**Also write** `test_a_closed_cap_gate_keeps_reconciliation_and_the_outbox_running` (a controller with a failing `CapTrader`: `reconcile_once` and `report_once` still call the trader; an exit signal's CLOSE is still submitted; a BUY judgment's Jev call is refused `BUDGET_CAP_UNKNOWN` and its follow-signal baseline is still enqueued) in `test_controller.py`.

- [ ] **Step 4: Run**

Run: `.venv/bin/python -m pytest tests/ai/runtime/test_controller.py tests/ai/runtime/test_budget_cap.py -q --timeout=60`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add trader/ai/controller.py trader/ai/budget_cap.py tests/ai/runtime/fakes.py tests/ai/runtime/test_controller.py tests/ai/runtime/test_budget_cap.py
git commit -m "feat: add the ai controller loops, slot cycles, atomic result commit and the owner cap read

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 10: `trader/ai_service.py`, the compose `ai` service and two-key mounts

**Files:**
- Create: `trader/ai_service.py`
- Modify: `docker-compose.yml` (service `ai`, volume `mmr_ai_data`)
- Modify: `docker.sh` (`KEYCHECK_SERVICES` adds `ai`; the key check also removes `${KEYCHECK_PROJECT}_mmr_ai_data`)
- Modify: `trader/messaging/principals.py` (`SERVICE_PRINCIPAL["ai"]`, `SERVICE_EXTRA_PRINCIPALS`, `service_principals`, `service_rpc_files`)
- Modify: `trader/messaging/keys_cli.py` (`_mount_problems` uses `service_rpc_files` and checks every principal of the service)
- Modify: `trader/messaging/rpc_keys.py` (`_LONG_LIVED_SERVICE_PRINCIPALS`, `_services_holding`)
- Modify (tests): `tests/test_compose_rpc_keys.py`, `tests/test_keys_check_mount.py`, `tests/test_docker_helper.py` (`KEYCHECK_SERVICES`), `tests/test_rpc_keys_init.py::test_restart_list_follows_the_peers`, `tests/test_compose_topology.py` (`PROCESS_SERVICES`, `expected_commands`)
- Create (tests): `tests/test_compose_ai_service.py`, `tests/ai/runtime/test_ai_service.py`, `tests/ai/runtime/test_runtime_isolation.py`

**Interfaces:**
- Consumes: everything above; Plan 4 `load_ai_config`, `check_credentials`, `AiStore`, `build_gateway`, `ReplayRecorder`, `SystemClock`, `AiConfigError`; `tests.compose_rpc_helpers`.
- Produces: `trader.ai_service` names of Cross-plan additions; `main(argv=None) -> int`; `DEFAULT_TRADER_ADDRESS = "tcp://127.0.0.1"`; `EXIT_REFUSED = 2`; principals helpers of Ruling 18.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_compose_ai_service.py
"""SP2 Plan 5 Task 10: the ai container holds model credentials only and keeps its data (spec 4, 12)."""
from pathlib import Path, PurePosixPath

import pytest
import yaml

from tests.compose_rpc_helpers import CONTAINER_CONFIG, ROOT, load_compose, visible_rpc_files, volumes
from trader.messaging.principals import service_rpc_files

ALLOWED_ENV = {"TZ", "PYTHONDONTWRITEBYTECODE", "TRADER_TYPED_ADDRESS", "OPENROUTER_API_KEY", "AWS_REGION",
               "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN", "AZURE_OPENAI_ENDPOINT",
               "AZURE_OPENAI_API_KEY", "AZURE_OPENAI_API_VERSION"}
FORBIDDEN_PREFIXES = ("ALPACA_", "IB_", "TWS_", "MASSIVE_", "TWELVEDATA_", "DASHBOARD_", "TELEGRAM")


@pytest.fixture(scope="module")
def ai():
    return load_compose()["services"]["ai"]


def test_ai_env_has_no_market_data_or_broker_credentials(ai):
    env = ai.get("environment") or {}
    assert not [name for name in env if name.startswith(FORBIDDEN_PREFIXES)]
    assert set(env) <= ALLOWED_ENV and "env_file" not in ai


def test_ai_service_does_not_merge_the_common_env():
    text = (ROOT / "docker-compose.yml").read_text()
    block = text.split("\n  ai:\n", 1)[1].split("\n  # ──", 1)[0]
    assert "*mmr-common-env" not in block                    # that anchor carries the Alpaca keys


def test_ai_sees_its_two_key_pairs_and_the_trader_public_key_only(ai):
    assert visible_rpc_files(ai) == service_rpc_files("ai") == {
        "ai_supervisor.key", "ai_supervisor.pub", "ai_research.key", "ai_research.pub", "trader.pub"}


def test_ai_reads_only_ai_yaml_from_the_config_dir(ai):
    mounted = {v["target"]: v for v in volumes(ai)}
    assert CONTAINER_CONFIG not in mounted                   # trader.yaml may hold alpaca_api_key_id
    ai_yaml = mounted[f"{CONTAINER_CONFIG}/ai.yaml"]
    assert ai_yaml["read_only"] and ai_yaml["source"] == "${HOME}/.config/mmr/ai.yaml"
    assert not [v for v in volumes(ai) if "secrets" in v["source"] or v["source"] == "mmr_db_data"]


def test_ai_data_is_a_named_volume_that_survives_recreation(ai):
    compose = load_compose()
    assert "mmr_ai_data" in compose["volumes"]
    data = [v for v in volumes(ai) if v["source"] == "mmr_ai_data"]
    assert len(data) == 1 and data[0]["type"] == "volume" and not data[0]["read_only"]
    default_path = yaml.safe_load((ROOT / "config_defaults" / "ai.yaml").read_text())["database_path"]
    assert str(PurePosixPath(default_path.replace("~", "/home/trader", 1)).parent) == data[0]["target"]
    assert data[0]["target"] not in (ai.get("tmpfs") or [])
    down = (ROOT / "docker.sh").read_text().split("\ndown() {", 1)[1].split("\n}", 1)[0]
    assert "--volumes" not in down and " -v" not in down     # ./docker.sh -d keeps every named volume


def test_ai_is_opt_in_publishes_nothing_and_runs_the_service(ai):
    assert ai["profiles"] == ["ai"] and not ai.get("ports")
    assert ai["command"] == ["python", "-m", "trader.ai_service"]
    assert "healthcheck" in ai and "trader" in ai["depends_on"]
```

```python
# tests/ai/runtime/test_runtime_isolation.py
"""The runtime modules may use typed RPC and the XNYS calendar, never the trading runtime or market data."""
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
FORBIDDEN = ("ib_async", "trader.trading", "trader.trader_service", "trader.data_providers", "trader.scoreboard",
             "trader.strategy", "alpaca")
MODULES = ("trader.ai.ids", "trader.ai.runtime_schema", "trader.ai.rpc_clients", "trader.ai.leadership",
           "trader.ai.schedule", "trader.ai.engine", "trader.ai.submitter", "trader.ai.outbox",
           "trader.ai.signal_intake", "trader.ai.controller", "trader.ai_service")


def test_the_runtime_stays_clear_of_the_trading_runtime():
    code = ("import sys, importlib\n"
            f"for name in {MODULES!r}:\n    importlib.import_module(name)\n"
            f"print(','.join(sorted(m for m in sys.modules if m.startswith({FORBIDDEN!r}))))\n")
    result = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == ""
```

```python
# tests/ai/runtime/test_ai_service.py
"""SP2 Plan 5 Task 10: the entry point fails loudly and installs nothing it should not."""
import logging

import pytest

from tests.ai.fakes import config_text
from tests.rpc_identity_fixtures import write_keyset
from trader.ai_service import EXIT_REFUSED, ServiceSettings, build_engine, main, run_service


def settings(tmp_path, **controller):
    keys = tmp_path / "keys"
    write_keyset(keys)
    block = f"database_path: {tmp_path / 'ai' / 'ai.duckdb'}\n"
    path = tmp_path / "ai.yaml"
    path.write_text(config_text(extra_top_level=block))
    return ServiceSettings(config_path=str(path), keys_dir=str(keys), trader_address="tcp://127.0.0.1")


def test_without_an_engine_the_service_refuses_to_start(tmp_path, caplog):
    caplog.set_level(logging.ERROR)
    code = run_service(settings(tmp_path), engine_factory=build_engine, environ={"OPENROUTER_API_KEY": "test-only"})
    assert code == EXIT_REFUSED and "no decision engine is installed" in caplog.text
    assert "test-only" not in caplog.text


def test_missing_credentials_stop_the_service_before_any_trader_call(tmp_path, caplog):
    caplog.set_level(logging.ERROR)
    called = []
    code = run_service(settings(tmp_path), engine_factory=lambda deps: called.append(deps), environ={})
    assert code == EXIT_REFUSED and called == []


def test_a_missing_config_file_exits_loudly(tmp_path, monkeypatch, caplog):
    caplog.set_level(logging.ERROR)
    monkeypatch.setenv("TRADER_TYPED_ADDRESS", "tcp://127.0.0.1")
    assert main(["--config", str(tmp_path / "missing.yaml")]) == EXIT_REFUSED
    assert "AI_CONFIG_NOT_FOUND" in caplog.text
```

Changes to existing tests (each assertion shown is the whole change):

- `tests/test_compose_rpc_keys.py`: import `service_principals, service_rpc_files`; `test_each_service_sees_only_its_own_key_pair_and_needed_public_keys` asserts `visible_rpc_files(compose["services"][name]) == service_rpc_files(name)`; in `test_no_service_mounts_another_principals_private_key` the allowed set becomes `{f"{p}.key" for p in service_principals(name)} if name in SERVICE_PRINCIPAL else set()`.
- `tests/test_keys_check_mount.py::_container_view`: `for name in (service_rpc_files(service) - set(missing)) | set(extra):`.
- `tests/test_docker_helper.py`: `KEYCHECK_SERVICES = {"trader", "strategy", "dashboard", "cli", "scheduler", "data", "ai"}`.
- `tests/test_rpc_keys_init.py::test_restart_list_follows_the_peers`: `RESTART_ON_ROTATE["ai_research"] == ("ai", "trader")` and add `RESTART_ON_ROTATE["ai_supervisor"] == ("ai", "trader")`; the strategy and cli rows are unchanged.
- `tests/test_compose_topology.py`: `PROCESS_SERVICES` adds `"ai"`; `expected_commands["ai"] = ["python", "-m", "trader.ai_service"]`.

- [ ] **Step 2: Run them and see them fail**

Run: `.venv/bin/python -m pytest tests/test_compose_ai_service.py tests/ai/runtime/test_ai_service.py tests/ai/runtime/test_runtime_isolation.py tests/test_compose_rpc_keys.py tests/test_keys_check_mount.py tests/test_docker_helper.py tests/test_rpc_keys_init.py tests/test_compose_topology.py -q --timeout=60`
Expected: `KeyError: 'ai'` in the compose tests, `ImportError: cannot import name 'service_rpc_files'`, `ModuleNotFoundError: No module named 'trader.ai_service'`.

- [ ] **Step 3: Implement**

`trader/messaging/principals.py`, after `SERVICE_PRINCIPAL` (which gains `"ai": "ai_supervisor"`):

```python
# A service that signs as more than one principal. SP2 spec 4: the ai service holds the
# ai_supervisor and ai_research keys (accepted limit: two keys in one process are not isolation).
SERVICE_EXTRA_PRINCIPALS: Mapping[str, tuple[str, ...]] = {"ai": ("ai_research",)}


def service_principals(service: str) -> tuple[str, ...]:
    own = SERVICE_PRINCIPAL[service]
    return () if own is None else (own, *SERVICE_EXTRA_PRINCIPALS.get(service, ()))


def service_rpc_files(service: str) -> frozenset[str]:
    """Key files a compose service must see: each own pair and every peer's .pub."""
    files: frozenset[str] = frozenset()
    for principal in service_principals(service):
        files |= rpc_files_for(principal)
    return files
```

`trader/messaging/keys_cli.py`, `_mount_problems`:

```python
def _mount_problems(service: str, keys_dir: Path, hmac_file: Path) -> list[str]:
    expected = service_rpc_files(service)
    seen = frozenset(os.listdir(keys_dir)) if keys_dir.is_dir() else frozenset()
    problems = [f"unexpected {name}" for name in sorted(seen - expected)]
    problems += [f"missing {name}" for name in sorted(expected - seen)]
    if not problems:
        for principal in service_principals(service):
            problems += _identity_problem(principal, keys_dir)
    ...                                                  # the HMAC checks are unchanged
```

(import `service_principals, service_rpc_files` next to `SERVICE_PRINCIPAL`; `rpc_files_for` is no longer imported there.)

`trader/messaging/rpc_keys.py`:

```python
_LONG_LIVED_SERVICE_PRINCIPALS: Mapping[str, tuple[str, ...]] = {
    "trader": ("trader",), "strategy": ("strategy",), "dashboard": ("dashboard",),
    "ai": ("ai_supervisor", "ai_research"),
}


def _services_holding(principal: str) -> tuple[str, ...]:
    return tuple(sorted(
        service for service, owns in _LONG_LIVED_SERVICE_PRINCIPALS.items()
        if principal in owns or any(principal in peers_for(own) for own in owns)
    ))
```

`docker.sh`: `KEYCHECK_SERVICES="trader strategy dashboard cli scheduler data ai"`, and after the existing `volume rm` line in `key_check`: `$RUNTIME volume rm "${KEYCHECK_PROJECT}_mmr_ai_data" >/dev/null 2>&1 || true`.

`docker-compose.yml`, a new service before `cli` and the volume at the end:

```yaml
  # ── ai: the AI paper controller (trader/ai_service.py, SP2 Plan 5) ─────────
  # Opt-in (profile "ai"): `docker compose --profile ai up -d ai` once the model ids in
  # ~/.config/mmr/ai.yaml are filled in (./docker.sh -u copies the template there). Holds
  # model-provider credentials only (spec 4): it does NOT inherit x-mmr-common-env (Alpaca
  # keys) and does NOT mount ~/.config/mmr (trader.yaml may hold Alpaca keys) -- only ai.yaml.
  # Two RPC key pairs (ai_supervisor, ai_research) plus trader.pub. Its state, ai.duckdb, lives
  # on the named volume mmr_ai_data and survives container recreation. No port is bound;
  # the healthcheck reads the heartbeat file the controller writes every 10 s.
  ai:
    <<: *mmr-hardening
    build: *mmr-build
    profiles: ["ai"]
    working_dir: /home/trader/mmr
    command: ["python", "-m", "trader.ai_service"]
    depends_on:
      - trader
    mem_limit: 1g
    cpus: 1.0
    environment:
      TZ: ${TIME_ZONE:-America/New_York}
      PYTHONDONTWRITEBYTECODE: "1"
      TRADER_TYPED_ADDRESS: tcp://trader
      OPENROUTER_API_KEY: ${OPENROUTER_API_KEY:-}
      AWS_REGION: ${AWS_REGION:-}
      AWS_ACCESS_KEY_ID: ${AWS_ACCESS_KEY_ID:-}
      AWS_SECRET_ACCESS_KEY: ${AWS_SECRET_ACCESS_KEY:-}
      AWS_SESSION_TOKEN: ${AWS_SESSION_TOKEN:-}
      AZURE_OPENAI_ENDPOINT: ${AZURE_OPENAI_ENDPOINT:-}
      AZURE_OPENAI_API_KEY: ${AZURE_OPENAI_API_KEY:-}
      AZURE_OPENAI_API_VERSION: ${AZURE_OPENAI_API_VERSION:-}
    volumes:
      - type: tmpfs
        target: /home/trader/.config/mmr/keys/rpc
        tmpfs:
          size: 65536
          mode: 0755
      - ${HOME}/.config/mmr/keys/rpc/ai_supervisor.key:/home/trader/.config/mmr/keys/rpc/ai_supervisor.key:ro
      - ${HOME}/.config/mmr/keys/rpc/ai_supervisor.pub:/home/trader/.config/mmr/keys/rpc/ai_supervisor.pub:ro
      - ${HOME}/.config/mmr/keys/rpc/ai_research.key:/home/trader/.config/mmr/keys/rpc/ai_research.key:ro
      - ${HOME}/.config/mmr/keys/rpc/ai_research.pub:/home/trader/.config/mmr/keys/rpc/ai_research.pub:ro
      - ${HOME}/.config/mmr/keys/rpc/trader.pub:/home/trader/.config/mmr/keys/rpc/trader.pub:ro
      - ${HOME}/.config/mmr/ai.yaml:/home/trader/.config/mmr/ai.yaml:ro
      - mmr_ai_data:/home/trader/.local/share/mmr_ai
      - ${HOME}/.local/share/mmr/logs:/home/trader/.local/share/mmr/logs
    healthcheck:
      test: ["CMD", "python3", "-c", "import os,sys,time; p='/tmp/mmr_ai_heartbeat.json'; sys.exit(0 if os.path.exists(p) and time.time() - os.path.getmtime(p) < 120 else 1)"]
      interval: 30s
      timeout: 5s
      retries: 3
      start_period: 120s
```

```yaml
volumes:
  mmr_db_data:
  mmr_ai_data:
```

```python
# trader/ai_service.py
"""The ai service: `python -m trader.ai_service` (SP2 Plan 5).

Holds model-provider credentials only (spec 4) and talks to the trader over
signed typed RPC as ai_supervisor and ai_research. It never publishes a risk
policy (spec 6.7) and never changes the budget cap from an AI path (spec 5.4):
the cap is the owner's trader.yaml value, read with get_ai_model_budget (Ruling 19).
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import signal
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Optional

from trader.ai.budget_cap import BudgetCapSync, CapGatedGateway
from trader.ai.clock import Clock, SystemClock
from trader.ai.config import DEFAULT_CONFIG_PATH, AiConfig, AiConfigError, check_credentials, load_ai_config
from trader.ai.controller import AiController, ExperimentWatch
from trader.ai.engine import DecisionEngine
from trader.ai.gateway import ModelCaller, build_gateway
from trader.ai.leadership import Leadership, new_holder_id
from trader.ai.outbox import ReportingOutbox
from trader.ai.replay import ReplayRecorder
from trader.ai.rpc_clients import AiRpcClients, ReadOnlySupervisor
from trader.ai.runtime_schema import ALL_MIGRATIONS
from trader.ai.schedule import SessionSlots
from trader.ai.signal_intake import SignalIntake
from trader.ai.store import AiStore
from trader.ai.submitter import Submitter

logger = logging.getLogger("trader.ai_service")

DEFAULT_TRADER_ADDRESS = "tcp://127.0.0.1"
EXIT_REFUSED = 2


class EngineNotInstalled(RuntimeError):
    pass


@dataclass(frozen=True)
class EngineDeps:
    config: AiConfig
    gateway: ModelCaller
    reads: ReadOnlySupervisor
    clock: Clock
    recorder: ReplayRecorder


def build_engine(deps: EngineDeps) -> DecisionEngine:
    """Plan 6 installs the real engine here (Ruling 16). Until then the service refuses to start."""
    raise EngineNotInstalled("no decision engine is installed (SP2 Plan 6); the ai service will not start")


@dataclass(frozen=True)
class ServiceSettings:
    config_path: str = DEFAULT_CONFIG_PATH
    keys_dir: Optional[str] = None
    trader_address: str = DEFAULT_TRADER_ADDRESS


async def serve(settings: ServiceSettings, *, engine_factory: Callable[[EngineDeps], Any], stop: asyncio.Event,
                clock: Optional[Clock] = None, environ: Optional[Mapping[str, str]] = None,
                wrap_clients: Optional[Callable[[AiRpcClients], AiRpcClients]] = None) -> None:
    clock = clock or SystemClock()
    environ = os.environ if environ is None else environ
    config = load_ai_config(settings.config_path)
    check_credentials(config, environ)                    # names missing variables only, never values
    cfg = config.controller
    Path(config.database_path).expanduser().parent.mkdir(parents=True, exist_ok=True)
    store = AiStore(config.database_path, clock=clock)
    await asyncio.to_thread(store.migrate, ALL_MIGRATIONS)
    clients = await asyncio.to_thread(lambda: AiRpcClients.connect(
        keys_dir=settings.keys_dir, address=settings.trader_address, query_port=cfg.trader_query_port,
        command_port=cfg.trader_command_port, timeout=cfg.rpc_timeout_seconds))
    if wrap_clients is not None:
        clients = wrap_clients(clients)
    try:
        raw_gateway = build_gateway(config, store=store, clock=clock, environ=environ)
        cap_sync = BudgetCapSync(supervisor=clients.supervisor, budget=raw_gateway.budget, clock=clock)
        gateway = CapGatedGateway(raw_gateway, cap_sync)  # no model call without a current owner cap
        engine = engine_factory(EngineDeps(config, gateway, ReadOnlySupervisor(clients.supervisor), clock,
                                           ReplayRecorder(store)))
        leadership = Leadership(supervisor=clients.supervisor, store=store, clock=clock, holder_id=new_holder_id(),
                                lease_seconds=cfg.lease_seconds, renew_seconds=cfg.renew_seconds,
                                held_retry_seconds=cfg.held_retry_seconds)
        clients.supervisor.bind_epoch(leadership.current_epoch)
        logger.info("waiting for the controller epoch as %s (up to one lease after a restart)",
                    leadership.holder_id)
        if await leadership.acquire(stop) is None:
            return
        await raw_gateway.start()                         # only the leader turns half-finished calls into UNKNOWN
        slots = SessionSlots(entry_minutes=cfg.entry_slot_minutes, position_minutes=cfg.position_slot_minutes,
                             grace_seconds=cfg.slot_start_grace_seconds)
        watch = ExperimentWatch(clients.supervisor)
        submitter = Submitter(store=store, supervisor=clients.supervisor, leadership=leadership, clock=clock,
                              slots=slots, experiment_state=watch.state,
                              not_found_settle_seconds=cfg.not_found_settle_seconds)
        controller = AiController(
            config=cfg, store=store, clock=clock, supervisor=clients.supervisor, leadership=leadership, watch=watch,
            submitter=submitter,
            outbox=ReportingOutbox(store=store, journal=gateway.journal, supervisor=clients.supervisor, clock=clock),
            intake=SignalIntake(store=store, supervisor=clients.supervisor, clock=clock,
                                page_limit=cfg.signal_page_limit, max_age_seconds=cfg.signal_max_age_seconds),
            slots=slots, engine=engine, gateway=gateway, cap_sync=cap_sync)
        await controller.run(stop)
    finally:
        clients.close()


def run_service(settings: ServiceSettings, *, engine_factory: Callable[[EngineDeps], Any] = build_engine,
                clock: Optional[Clock] = None, environ: Optional[Mapping[str, str]] = None,
                wrap_clients: Optional[Callable[[AiRpcClients], AiRpcClients]] = None) -> int:
    async def main_task() -> None:
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for signum in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(signum, stop.set)
        await serve(settings, engine_factory=engine_factory, stop=stop, clock=clock, environ=environ,
                    wrap_clients=wrap_clients)
    try:
        asyncio.run(main_task())
    except EngineNotInstalled as exc:
        logger.error("%s", exc)
        return EXIT_REFUSED
    except AiConfigError as exc:
        logger.error("ai service refused to start: %s", exc.code)
        return EXIT_REFUSED
    return 0


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m trader.ai_service")
    parser.add_argument("--config", default=DEFAULT_CONFIG_PATH)
    parser.add_argument("--keys-dir", default=None)
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    settings = ServiceSettings(config_path=args.config, keys_dir=args.keys_dir,
                               trader_address=os.environ.get("TRADER_TYPED_ADDRESS", DEFAULT_TRADER_ADDRESS))
    return run_service(settings)


if __name__ == "__main__":
    sys.exit(main())
```

`check_credentials` raises `AiConfigError` (Plan 4 Task 1) for missing env names, so missing credentials exit with code 2 before any socket opens and before the engine is built (`test_missing_credentials_stop_the_service_before_any_trader_call`). If Plan 4's `DEFAULT_CONFIG_PATH` is not importable from `trader.ai.config`, use the literal `"~/.config/mmr/ai.yaml"` it names.

- [ ] **Step 4: Run**

Run: `.venv/bin/python -m pytest tests/test_compose_ai_service.py tests/ai/runtime/test_ai_service.py tests/ai/runtime/test_runtime_isolation.py tests/test_compose_rpc_keys.py tests/test_keys_check_mount.py tests/test_docker_helper.py tests/test_rpc_keys_init.py tests/test_compose_topology.py tests/ai/test_isolation.py -q --timeout=60`
Expected: all pass. If `test_docker_helper.py` pins the exact cleanup commands of `-K`, add the `mmr_ai_data` removal line to that expectation too.

- [ ] **Step 5: Commit**

```bash
git add trader/ai_service.py docker-compose.yml docker.sh trader/messaging/principals.py trader/messaging/keys_cli.py trader/messaging/rpc_keys.py tests/test_compose_ai_service.py tests/ai/runtime/test_ai_service.py tests/ai/runtime/test_runtime_isolation.py tests/test_compose_rpc_keys.py tests/test_keys_check_mount.py tests/test_docker_helper.py tests/test_rpc_keys_init.py tests/test_compose_topology.py
git commit -m "feat: add the ai service entry point and its opt-in compose container

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 11: Crash, leadership and durability against SP1's real coordinator

**Files:**
- Create (tests): `tests/ai/runtime/trader_world.py`, `tests/ai/runtime/test_controller_integration.py`

**Interfaces:**
- Consumes: `tests/sp1_fixtures.py` (`served_stack`, `LoopThread`, `ServedStack.advance_and_promote`, `ServedStack.sockets`, `ServedStack.experiment_id`), `tests/sp1_acceptance/test_acceptance_run.py` (`settings`, `market`, `entries_placed`), `trader.acceptance.scenario.deployment_record`, `trader.automation.risk_limits.PAPER_LIMITS`, `trader.automation.ai_paper_actions.PUBLISH_ACTION`; Plan 1 (`publish_ai_risk_policy` for `cli`, `get_ai_paper_decision`, `DecisionRow.controller_epoch`, `StrategySignalRecord`, `SignalEntry`, `ServedStack.signed(..., controller_epoch=...)`); Plan 2 (`record_ai_cost`, `record_simulated_decision`, `get_scoreboard` `benchmarks.ai_cost` / `benchmarks.books`); `tests.rpc_identity_fixtures.write_keyset`, `make_identities`; Tasks 2–10.
- Produces: test helpers `TraderClock`, `FlakyClient`, `TraderWorld`, `AiNode`, `write_service_config`, `wait_for_heartbeat` (Task 12 and Plan 6 reuse them).

Spec 12 asks each crash test to show: the original command id and exact body survive, the successor reconciles under its new epoch, the original receipt is recovered, no duplicate entry, the position stays protected, and accepted trader work continues through the takeover. The stack is SP1's real coordinator, risk gates, ownership, protective saga and reconciler (`tests/sp1_fixtures.py`, the composition `tests/test_safe_close_integration.py` uses); only the broker is simulated.

- [ ] **Step 1: Write the helpers and the failing tests**

```python
# tests/ai/runtime/trader_world.py
"""The Plan 5 runtime against SP1's real served trader: real coordinator, risk gates, ownership, saga and
reconciler over signed typed RPC. Only the broker is simulated (BrokerSim)."""
from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Any, Callable

from tests.ai.fakes import config_text
from tests.sp1_acceptance.test_acceptance_run import entries_placed, market, settings as acceptance_settings
from tests.sp1_fixtures import served_stack
from trader.acceptance.scenario import deployment_record
from trader.ai.controller import ExperimentWatch
from trader.ai.engine import ProposedDecision
from trader.ai.journal import AttemptJournal
from trader.ai.leadership import Leadership, new_holder_id
from trader.ai.outbox import ReportingOutbox
from trader.ai.rpc_clients import AiRpcClients
from trader.ai.runtime_schema import ALL_MIGRATIONS
from trader.ai.schedule import SessionSlots
from trader.ai.store import AiStore
from trader.ai.submitter import Submitter
from trader.automation.risk_limits import PAPER_LIMITS


class TraderClock:
    """The ai side reads the served trader's clock, so both sides agree on time.

    ``frozen_monotonic`` plays a paused process: its own lease deadline never passes.
    ``real_sleep`` is for a whole service in the test loop (its loops must not move trader time)."""

    def __init__(self, served, *, frozen_monotonic=False, real_sleep=False):
        self._served, self._origin = served, served.now()
        self._frozen, self._real_sleep = frozen_monotonic, real_sleep

    def now(self):
        return self._served.now()

    def monotonic(self):
        return 0.0 if self._frozen else (self._served.now() - self._origin).total_seconds()

    async def sleep(self, seconds):
        if self._real_sleep:
            await asyncio.sleep(seconds)
        else:
            self._served.advance(seconds)
            await asyncio.sleep(0)


class FlakyClient:
    """Wraps a real TypedRpcClient. "down": refused before the send (ConnectionError, like IMMEDIATE=1);
    "lose_reply": the trader handles the request, then the reply is lost (TimeoutError)."""

    def __init__(self, inner):
        self.inner, self.steps = inner, {}

    def script(self, method, *steps):
        self.steps.setdefault(method, []).extend(steps)

    def call(self, method, body, response_model, timeout=None, **options):
        queue = self.steps.get(method) or []
        step = queue.pop(0) if queue else None
        if step == "down":
            raise ConnectionError(f"typed RPC call to {method!r} could not be sent: no route to server")
        reply = self.inner.call(method, body, response_model, timeout, **options)
        if step == "lose_reply":
            raise TimeoutError(f"typed RPC call to {method!r} timed out")
        return reply

    def close(self):
        self.inner.close()


class TraderWorld:
    """Served SP1 trader, ARMED experiment, operator policy (cli) and a registered deployment."""

    def __init__(self, tmp_path, loop_thread, monkeypatch, *, identities=None):
        options = {} if identities is None else {"identities": identities}
        self.served = served_stack(tmp_path, loop_thread, monkeypatch, **options)
        market(self.served)
        chosen = acceptance_settings()
        self.conid, self.quantity = chosen.conid_s, chosen.quantity_s
        published = self.served.call("cli", "publish_ai_risk_policy", {
            "command_id": "cli-pol-000000000001", "limits": PAPER_LIMITS.to_json(),
            "reason": "operator initial policy"})
        assert published["state"] == "RESOLVED", published
        self.policy_revision = published["outcome"]["revision"]
        registered = self.served.call("ai_research", "register_ai_deployment",
                                      {"deployment": deployment_record(chosen)})
        self.digest = registered["outcome"]["digest"]
        (tmp_path / "ai").mkdir(exist_ok=True)
        self.ai_db = tmp_path / "ai" / "ai.duckdb"

    def enter(self, action_key=None) -> ProposedDecision:
        ask = self.served.sim.quotes[self.conid][1]
        return ProposedDecision(action_key=action_key or f"enter:{self.conid}", action="ENTER", conid=self.conid,
                                side="BUY", decider="jev", evidence_digest="sha256:" + "e" * 64,
                                deployment_digest=self.digest, policy_revision=self.policy_revision,
                                stop_price=round(ask * 0.98, 2), target_price=round(ask * 1.02, 2),
                                quantity=self.quantity)

    def node(self, *, clock=None, flaky=False) -> "AiNode":
        return AiNode(self, clock or TraderClock(self.served), flaky)

    def advance(self, seconds):
        self.served.advance_and_promote(seconds)        # time passes, the broker moves, the trader catches up

    def settle(self, max_steps=30):
        """Let the broker fill and the saga protect, one promoted second at a time, until protected."""
        for _ in range(max_steps):
            self.served.advance_and_promote(1.0)
            if self.entries() and self.protected():
                return
        raise AssertionError(f"not protected after {max_steps} steps; entries: {self.entries()}")

    def entries(self):
        return entries_placed(self.served)

    def protected(self) -> bool:
        rows = self.served.call("ai_supervisor", "get_broker_order_evidence", {"conid": self.conid})["orders"]
        return {"stop", "take_profit"} <= {r["leg"] for r in rows if r["status"] in ("Submitted", "PreSubmitted")}

    def receipt(self, decision_id):
        return self.served.stack.coordinator.get_command(f"aip-{decision_id}")

    def decision_row(self, decision_id):
        return self.served.stack.ai_paper.decision_store.row(decision_id)

    def strategy_signal(self) -> str:
        """The strategy service's side: a BUY into the durable signal record (Plan 1 Task 6)."""
        from trader.data.duckdb_store import DuckDBConnection
        from trader.data.strategy_signal_record import SignalEntry, StrategySignalRecord
        entry = SignalEntry.create(strategy_name="orb", conid=self.conid, action="BUY", probability=0.7,
                                   signal_time=self.served.now())
        StrategySignalRecord(DuckDBConnection.get_instance(self.served.stack.ai_paper.signals_path),
                             now=self.served.now).append(entry)
        return entry.source_event_id

    def close(self):
        self.served.close()


class AiNode:
    """One ai process's runtime parts on the shared ai.duckdb; tests drive them step by step."""

    def __init__(self, world: TraderWorld, clock, flaky: bool):
        self.world, self.clock = world, clock
        self.store = AiStore(world.ai_db, clock=clock)
        self.store.migrate(ALL_MIGRATIONS)
        sockets = world.served.sockets
        raw = {"supervisor_command": sockets.client("ai_supervisor", "trader", "command", timeout=30.0),
               "supervisor_query": sockets.client("ai_supervisor", "trader", "query", timeout=30.0),
               "supervisor_discovery": sockets.client("ai_supervisor", "trader", "query", timeout=30.0),
               "research_command": sockets.client("ai_research", "trader", "command", timeout=30.0),
               "research_query": sockets.client("ai_research", "trader", "query", timeout=30.0)}
        self.sockets = {name: FlakyClient(client) for name, client in raw.items()} if flaky else raw
        self.clients = AiRpcClients.from_sockets(**self.sockets, timeout=30.0)
        self.leadership = Leadership(supervisor=self.clients.supervisor, store=self.store, clock=clock,
                                     holder_id=new_holder_id())
        self.clients.supervisor.bind_epoch(self.leadership.current_epoch)
        self.watch = ExperimentWatch(self.clients.supervisor)
        self.submitter = Submitter(store=self.store, supervisor=self.clients.supervisor, leadership=self.leadership,
                                   clock=clock, slots=SessionSlots(), experiment_state=self.watch.state)
        self.outbox = ReportingOutbox(store=self.store, journal=AttemptJournal(self.store),
                                      supervisor=self.clients.supervisor, clock=clock)

    async def plan(self, decision, *, source_id="sig-" + "7" * 32, ttl=300) -> str:
        import datetime as dt
        now = self.clock.now()
        return await self.store.atransaction(lambda conn: self.submitter.insert_in_tx(
            conn, source_kind="entry_signal", source_id=source_id, decision=decision,
            expires_at=now + dt.timedelta(seconds=ttl), epoch=self.leadership.last_epoch, now=now))


def write_service_config(tmp_path: Path, world: TraderWorld, heartbeat: Path) -> Path:
    """ai.yaml for a whole service against the served trader: its ports, fast loops, the shared ai.duckdb."""
    ports = world.served.sockets.ports
    block = (f"database_path: {world.ai_db}\n"
             "controller:\n"
             f"  trader_query_port: {ports[('trader', 'query')]}\n"
             f"  trader_command_port: {ports[('trader', 'command')]}\n"
             "  rpc_timeout_seconds: 30\n  renew_seconds: 5\n  held_retry_seconds: 0.2\n"
             "  signal_poll_seconds: 0.2\n  reconcile_seconds: 0.2\n  outbox_seconds: 0.2\n"
             "  experiment_poll_seconds: 0.2\n  heartbeat_seconds: 0.2\n"
             f"  heartbeat_path: {heartbeat}\n")
    path = tmp_path / "ai.yaml"
    path.write_text(config_text(extra_top_level=block))
    return path


async def wait_for_heartbeat(path: Path, matches: Callable[[dict], bool], task: Any = None,
                             timeout: float = 30.0) -> dict:
    status, deadline = None, time.monotonic() + timeout
    while time.monotonic() < deadline:
        if task is not None and task.done():
            task.result()
            raise AssertionError("the ai service stopped early")
        try:
            status = json.loads(path.read_text())
        except (FileNotFoundError, json.JSONDecodeError):
            status = None
        if status is not None and matches(status):
            return status
        await asyncio.sleep(0.05)
    raise AssertionError(f"the heartbeat never matched; last: {status}")
```

```python
# tests/ai/runtime/test_controller_integration.py
"""SP2 Plan 5 Task 11: spec 12 "Crash and leadership" 1, 3, 4, "Signed epoch" and "Durability" against SP1's
real coordinator over signed typed RPC. Crash test 2 (a real process restart) is Task 12."""
import asyncio
import json

import pytest

from tests.ai.runtime.fakes import write_cost_event
from tests.ai.runtime.scripted_engine import ScriptedEngine
from tests.ai.runtime.trader_world import TraderClock, TraderWorld, wait_for_heartbeat, write_service_config
from tests.rpc_identity_fixtures import make_identities, write_keyset
from tests.sp1_fixtures import LoopThread
from trader.ai.engine import SimulatedBaseline
from trader.ai.leadership import NotLeader
from trader.ai.model_client import Usage
from trader.ai.outbox import register_context_in_tx
from trader.ai_service import ServiceSettings, serve
from trader.automation.ai_paper_actions import PUBLISH_ACTION
from trader.messaging.typed_rpc import TypedRpcRemoteError


@pytest.fixture
def loop_thread():
    thread = LoopThread()
    yield thread
    thread.stop()


@pytest.fixture
def world(tmp_path, loop_thread, monkeypatch):
    created = TraderWorld(tmp_path, loop_thread, monkeypatch)
    yield created
    created.close()


@pytest.mark.asyncio
async def test_crash_with_the_journal_committed_and_the_send_not_started(world):              # spec 12 crash 1
    a = world.node()
    assert await a.leadership.acquire() == 1
    decision_id = await a.plan(world.enter())
    before = await a.submitter.get(decision_id)
    # the process dies here: nothing left the process
    world.advance(61)
    b = world.node()
    assert await b.leadership.acquire() == 2
    assert await b.submitter.recover() == 0
    await b.watch.refresh()
    await b.submitter.send_due()
    after = await b.submitter.get(decision_id)
    assert (after.body_sha256, after.created_epoch, after.last_epoch) == (before.body_sha256, 1, 2)
    assert after.state in ("ACCEPTED", "FINAL") and after.receipt_state == world.receipt(decision_id).state
    assert world.decision_row(decision_id).controller_epoch == 2
    replay = await b.clients.supervisor.call("submit_ai_paper_decision", json.loads(after.body_json))
    assert replay["command_id"] == f"aip-{decision_id}"           # same id and body: a replay, never a conflict
    world.settle()
    assert len(world.entries()) == 1 and world.protected()


@pytest.mark.asyncio
async def test_takeover_around_a_lost_submit_reply(world):                                      # spec 12 crash 3
    a = world.node(flaky=True)
    a.sockets["supervisor_command"].script("submit_ai_paper_decision", "lose_reply")
    assert await a.leadership.acquire() == 1
    await a.watch.refresh()
    decision_id = await a.plan(world.enter())
    await a.submitter.send_due()
    assert (await a.submitter.get(decision_id)).state == "UNKNOWN"
    assert world.receipt(decision_id) is not None                  # the trader did accept it
    world.advance(61)
    b = world.node()
    assert await b.leadership.acquire() == 2
    await b.submitter.reconcile_once()                             # by the original id, under epoch 2
    row = await b.submitter.get(decision_id)
    assert row.state in ("ACCEPTED", "FINAL") and row.receipt_state == world.receipt(decision_id).state
    assert (row.attempts, row.last_epoch) == (1, 1)                 # recovered, never resent
    world.settle()                                                 # accepted trader work went on through the takeover
    assert len(world.entries()) == 1 and world.protected()
    assert a.leadership.current_epoch() is None
    with pytest.raises(NotLeader):
        await a.leadership.grant_once()


@pytest.mark.asyncio
async def test_stale_controller_is_refused_by_its_epoch(world):                                # spec 12 crash 4
    a = world.node(clock=TraderClock(world.served, frozen_monotonic=True))     # paused: its lease looks alive
    assert await a.leadership.acquire() == 1
    await a.watch.refresh()
    world.advance(61)
    b = world.node()
    assert await b.leadership.acquire() == 2
    decision_id = await a.plan(world.enter())
    await a.submitter.send_due()
    row = await a.submitter.get(decision_id)
    assert (row.state, row.error_code) == ("PENDING", "CONTROLLER_EPOCH_STALE")
    assert a.leadership.current_epoch() is None and world.receipt(decision_id) is None   # no ledger row
    await b.watch.refresh()
    await b.submitter.send_due()                                   # the successor sends the same id and body
    row = await b.submitter.get(decision_id)
    assert row.state in ("ACCEPTED", "FINAL") and row.last_epoch == 2
    assert world.decision_row(decision_id).controller_epoch == 2
    world.settle()
    assert len(world.entries()) == 1 and world.protected()


@pytest.mark.asyncio
async def test_a_missing_or_altered_epoch_is_refused(world):                                  # spec 12 signed epoch
    a = world.node()
    await a.leadership.acquire()
    decision_id = await a.plan(world.enter())
    body = json.loads((await a.submitter.get(decision_id)).body_json)
    sockets = world.served.sockets
    with pytest.raises(TypedRpcRemoteError) as exc:
        sockets.client("ai_supervisor", "trader", "command").call("submit_ai_paper_decision", body, dict)
    assert exc.value.code == "CONTROLLER_EPOCH_MISSING"
    world.advance(61)
    b = world.node()
    assert await b.leadership.acquire() == 2
    stale = sockets.signed("ai_supervisor", role="command", method="submit_ai_paper_decision", body=body,
                           controller_epoch=1)
    assert sockets.raw_code(stale.model_copy(update={"controller_epoch": 2})) == "AUTHENTICATION_ERROR"
    assert sockets.raw_code(stale) == "CONTROLLER_EPOCH_STALE"
    assert world.receipt(decision_id) is None


@pytest.mark.asyncio
async def test_outbox_delivers_after_a_trader_outage_without_duplicates(world):           # spec 12 durability
    node = world.node(flaky=True)
    world.served.advance(1)
    now, experiment_id, context = node.clock.now(), world.served.experiment_id, "sig-" + "5" * 32
    await node.store.atransaction(lambda conn: register_context_in_tx(
        conn, context_key=context, experiment_id=experiment_id, served_kind="signal", served_id=context, now=now))
    no_trade = SimulatedBaseline("no_trade.v1", "self_found", "cyc-entry-20260717-1100", now)
    await node.store.atransaction(lambda conn: node.outbox.enqueue_simulated_in_tx(
        conn, experiment_id=experiment_id, baseline=no_trade, wait_for_decision_id=None, now=now))
    write_cost_event(node.store, request_key=f"{context}/jev/1", kind="CONFIRMED", cost_micros=15_000,
                     usage=Usage(1000, 200), now=now)
    await node.outbox.pump_costs()
    command = node.sockets["supervisor_command"]
    command.script("record_simulated_decision", "lose_reply")
    command.script("record_ai_cost", "down", "lose_reply")
    for _ in range(4):
        await node.outbox.deliver_due()
        world.served.advance(20)                                   # past every backoff
    assert await node.outbox.counts() == {"waiting": 0, "pending": 0, "delivered": 2, "dead": 0}
    report = world.served.call("cli", "get_scoreboard", {})
    assert report["benchmarks"]["ai_cost"]["calls"] == 1
    books = [b for b in report["benchmarks"]["books"] if b["baseline_id"] == "no_trade.v1"]
    assert len(books) == 1 and books[0]["records"] == 1


@pytest.fixture
def keyed_world(tmp_path, loop_thread, monkeypatch):
    keys = tmp_path / "keys"
    created = TraderWorld(tmp_path, loop_thread, monkeypatch, identities=make_identities(keys=write_keyset(keys)))
    created.keys_dir = keys
    yield created
    created.close()


@pytest.mark.asyncio
async def test_the_ai_service_never_publishes_policy_on_start_or_restart(keyed_world, tmp_path):   # spec 6.7
    world, heartbeat = keyed_world, tmp_path / "hb.json"
    settings = ServiceSettings(config_path=str(write_service_config(tmp_path, world, heartbeat)),
                               keys_dir=str(world.keys_dir), trader_address="tcp://127.0.0.1")
    for epoch in (1, 2):                                           # a start, then a restart as a new holder
        stop = asyncio.Event()
        task = asyncio.create_task(serve(settings, engine_factory=lambda deps: ScriptedEngine(), stop=stop,
                                         clock=TraderClock(world.served, real_sleep=True),
                                         environ={"OPENROUTER_API_KEY": "test-only-not-a-key"}))
        await wait_for_heartbeat(heartbeat, lambda status, e=epoch: status["epoch"] == e, task)
        stop.set()
        await asyncio.wait_for(task, timeout=15)
        world.advance(61)
    sources = world.served.trader.journal_db.execute(
        "SELECT source FROM command_ledger WHERE action = ?", [PUBLISH_ACTION], fetch="all")
    assert sources == [("cli",)]                                   # only the operator's initial policy


@pytest.mark.asyncio
async def test_the_owner_cap_is_read_from_trader_yaml_over_signed_rpc(keyed_world, tmp_path):   # Ruling 19
    world, heartbeat = keyed_world, tmp_path / "hb.json"
    world.served.trader.ai_paper_config = replace(world.served.trader.ai_paper_config, model_budget_usd_per_day=1500.0)
    world.reregister_trader_surfaces()             # the query reads the value once at registration, like a restart
    settings = ServiceSettings(config_path=str(write_service_config(tmp_path, world, heartbeat)),
                               keys_dir=str(world.keys_dir), trader_address="tcp://127.0.0.1")
    stop = asyncio.Event()
    task = asyncio.create_task(serve(settings, engine_factory=lambda deps: ScriptedEngine(), stop=stop,
                                     clock=TraderClock(world.served, real_sleep=True),
                                     environ={"OPENROUTER_API_KEY": "test-only-not-a-key"}))
    await wait_for_heartbeat(heartbeat, lambda status: status.get("budget_cap_ready") is True, task)
    stop.set()
    await asyncio.wait_for(task, timeout=15)
    snapshot = await Budget(AiStore(world.ai_database_path, clock=TraderClock(world.served)),
                            TraderClock(world.served), calls_per_hour=120).snapshot()
    assert snapshot.effective_cap_micros == 1_500_000_000
```

`TraderWorld.reregister_trader_surfaces()` rebuilds the served trader's typed RPC registries (the same call `served_stack` makes at start), and `TraderWorld.ai_database_path` is the `database_path` that `write_service_config` puts in the ai config; add both to `trader_world.py` if they are missing (`replace` is `dataclasses.replace`; `Budget`, `AiStore` from Plan 4).

- [ ] **Step 2: Run them**

Run: `.venv/bin/python -m pytest tests/ai/runtime/test_controller_integration.py -q --timeout=120`
Expected: all pass (Tasks 2–10 are in place; this task adds tests only). A failure here is a real integration defect: fix the runtime, never the assertion. If `acceptance_settings().conid_s` has no quote in `market(...)`, add one `served.sim.quote(conid, bid, ask)` line in `TraderWorld.__init__`.

- [ ] **Step 3: Commit**

```bash
git add tests/ai/runtime/trader_world.py tests/ai/runtime/test_controller_integration.py
git commit -m "test: pin ai crash, takeover, signed epoch and outbox durability on the sp1 stack

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 12: A real `ai` process killed after the trader accepted, then restarted; docs; full suite

**Files:**
- Create (tests): `tests/ai/runtime/child_service.py`, `tests/ai/runtime/test_crash_restart_subprocess.py`
- Modify: `AGENTS.md` (Architecture list: the `ai` service), `docs/OPERATIONAL_STATE.md` (how to start and stop the `ai` profile)

**Interfaces:**
- Consumes: `run_service`, `ServiceSettings` (Task 10); `ScriptedEngine.from_file` (Task 5); `TraderWorld`, `write_service_config` (Task 11); `derive_decision_id`; `write_keyset`, `make_identities`.
- Produces: the spec 12 key test "Trader accepted, receipt not saved" with a real process restart.

The child is the real service (`run_service`: real config load, real key files, real `ai.duckdb`, real leadership, real loops). It differs from production in two places only: its clock starts at the served trader's time, and with `--block-after-submit` it freezes right after the trader's submit reply arrives and before the receipt is written, so the parent can kill it at exactly that point with `SIGKILL`.

- [ ] **Step 1: Write the child and the failing test**

```python
# tests/ai/runtime/child_service.py
"""The real ai service in a child process, for spec 12 crash test 2 (run as `python -m`)."""
from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import json
import logging
import sys
import time
from pathlib import Path

from tests.ai.runtime.scripted_engine import ScriptedEngine
from trader.ai_service import ServiceSettings, run_service


class OffsetClock:
    """Real time, shifted so the child starts at the served trader's instant."""

    def __init__(self, start: dt.datetime):
        self._start, self._t0 = start, time.monotonic()

    def now(self) -> dt.datetime:
        return self._start + dt.timedelta(seconds=time.monotonic() - self._t0)

    def monotonic(self) -> float:
        return time.monotonic()

    async def sleep(self, seconds: float) -> None:
        await asyncio.sleep(seconds)


def block_after_submit(marker: Path):
    """Once the trader's reply to a submit arrives: write it to the marker and hang. The receipt is never saved."""
    def wrap(clients):
        real_call = clients.supervisor.call

        async def call(method, body, *, epoch=None):
            reply = await real_call(method, body, epoch=epoch)
            if method == "submit_ai_paper_decision":
                marker.write_text(json.dumps(reply))
                await asyncio.Event().wait()
            return reply
        clients.supervisor.call = call
        return clients
    return wrap


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True)
    parser.add_argument("--keys-dir", required=True)
    parser.add_argument("--clock-start", required=True)
    parser.add_argument("--script", required=True)
    parser.add_argument("--block-after-submit")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    marker = Path(args.block_after_submit) if args.block_after_submit else None
    return run_service(ServiceSettings(config_path=args.config, keys_dir=args.keys_dir,
                                       trader_address="tcp://127.0.0.1"),
                       engine_factory=lambda deps: ScriptedEngine.from_file(args.script),
                       clock=OffsetClock(dt.datetime.fromisoformat(args.clock_start)),
                       wrap_clients=block_after_submit(marker) if marker else None)


if __name__ == "__main__":
    sys.exit(main())
```

```python
# tests/ai/runtime/test_crash_restart_subprocess.py
"""SP2 spec 12, crash test 2: the trader accepted a decision, the ai process died before saving the receipt.
This test terminates and restarts the ai process as a real OS process (SIGKILL, then a fresh process)."""
import dataclasses
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from tests.ai.runtime.trader_world import TraderWorld, write_service_config
from tests.rpc_identity_fixtures import make_identities, write_keyset
from tests.sp1_fixtures import LoopThread
from trader.ai.ids import derive_decision_id
from trader.data.duckdb_store import DuckDBConnection

ROOT = Path(__file__).resolve().parents[3]
pytestmark = pytest.mark.timeout(240)


@pytest.fixture
def loop_thread():
    thread = LoopThread()
    yield thread
    thread.stop()


def spawn(args, log: Path) -> subprocess.Popen:
    env = {**os.environ, "OPENROUTER_API_KEY": "test-only-not-a-key", "PYTHONPATH": str(ROOT)}
    return subprocess.Popen([sys.executable, "-m", "tests.ai.runtime.child_service", *args], cwd=ROOT, env=env,
                            stdout=log.open("w"), stderr=subprocess.STDOUT)


def wait_until(check, process: subprocess.Popen, log: Path, timeout: float = 90.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        found = check()
        if found:
            return found
        if process.poll() is not None:
            raise AssertionError(f"the ai process exited with {process.returncode}:\n{log.read_text()[-4000:]}")
        time.sleep(0.1)
    raise AssertionError(f"timed out:\n{log.read_text()[-4000:]}")


def heartbeat(path: Path):
    try:
        return json.loads(path.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def submission_row(db: Path, decision_id: str) -> dict:
    columns = ("state", "receipt_state", "created_epoch", "last_epoch", "attempts", "body_sha256")
    row = DuckDBConnection.get_instance(str(db)).execute(
        f"SELECT {', '.join(columns)} FROM ai_submissions WHERE decision_id = ?", [decision_id], fetch="one")
    return dict(zip(columns, row))


def test_trader_accepted_receipt_not_saved_survives_a_real_process_restart(tmp_path, loop_thread, monkeypatch):
    keys = tmp_path / "keys"
    world = TraderWorld(tmp_path, loop_thread, monkeypatch, identities=make_identities(keys=write_keyset(keys)))
    processes = []
    try:
        beat, marker = tmp_path / "hb.json", tmp_path / "sent.json"
        config = write_service_config(tmp_path, world, beat)
        decision = world.enter()
        script = tmp_path / "script.json"
        script.write_text(json.dumps({"entry_signal": [dataclasses.asdict(decision)]}))
        source_event_id = world.strategy_signal()
        decision_id = derive_decision_id(source_event_id, decision.action_key)
        args = ["--config", str(config), "--keys-dir", str(keys), "--clock-start", world.served.now().isoformat(),
                "--script", str(script)]

        first = spawn([*args, "--block-after-submit", str(marker)], tmp_path / "first.log")
        processes.append(first)
        wait_until(marker.exists, first, tmp_path / "first.log")
        accepted = json.loads(marker.read_text())
        assert accepted["command_id"] == f"aip-{decision_id}" and world.receipt(decision_id) is not None
        first.send_signal(signal.SIGKILL)                          # terminated: no cleanup, no receipt write
        assert first.wait(timeout=10) == -signal.SIGKILL

        world.advance(61)                                          # the dead process's lease ends on the trader
        log = tmp_path / "second.log"
        second = spawn(args, log)
        processes.append(second)
        wait_until(lambda: (status := heartbeat(beat)) and status["epoch"] == 2
                   and status["unsettled_submissions"] == 0, second, log)
        second.terminate()
        assert second.wait(timeout=30) == 0

        row = submission_row(world.ai_db, decision_id)
        assert row["state"] in ("ACCEPTED", "FINAL") and row["receipt_state"] is not None
        assert (row["created_epoch"], row["last_epoch"], row["attempts"]) == (1, 1, 1)   # reconciled, never resent
        assert world.decision_row(decision_id).controller_epoch == 1                   # the original admission
        opportunities = DuckDBConnection.get_instance(str(world.ai_db)).execute(
            "SELECT state FROM ai_opportunities", fetch="all")
        assert opportunities == [("DECIDED",)]                    # the signal was not judged a second time
        world.settle()
        assert len(world.entries()) == 1 and world.protected()
    finally:
        for process in processes:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=10)
        world.close()
```

- [ ] **Step 2: Run it**

Run: `.venv/bin/python -m pytest tests/ai/runtime/test_crash_restart_subprocess.py -q --timeout=240`
Expected: pass. The two child logs (`first.log`, `second.log` in the test's `tmp_path`) are printed in the assertion message on any failure. If the child cannot import `tests.*`, check that `tests/__init__.py`, `tests/ai/__init__.py` and `tests/ai/runtime/__init__.py` exist.

- [ ] **Step 3: Docs**

`AGENTS.md`, in the Architecture service list after **scheduler**:

```markdown
- **ai** (`trader.ai_service`, compose profile `ai`, opt-in): the AI paper controller. Holds model-provider keys only (no Alpaca, no IB, no `trader.yaml`); signs as `ai_supervisor` and `ai_research`; its state is `ai.duckdb` on the `mmr_ai_data` volume. After a restart it waits up to one lease (60 s) for the trader-granted controller epoch. It never publishes a risk policy.
```

and in the ports table nothing changes (the `ai` service binds no port).

`docs/OPERATIONAL_STATE.md`, in the runbook section next to the SP1 acceptance line Plan 1 added:

```markdown
- AI paper controller (SP2): `./docker.sh -u` copies `ai.yaml` to `~/.config/mmr/` once; fill in the model ids and prices, then `docker compose --profile ai up -d ai`. Stop it with `docker compose --profile ai stop ai` (always before the SP1 acceptance run). Its health is the heartbeat file `/tmp/mmr_ai_heartbeat.json` inside the container. Its data volume `mmr_ai_data` is kept by `./docker.sh -d`; only `./docker.sh -c` removes volumes.
```

- [ ] **Step 4: Run every Plan 5 test, then the full suite once**

Run: `.venv/bin/python -m pytest tests/ai tests/test_compose_ai_service.py tests/test_compose_rpc_keys.py tests/test_keys_check_mount.py tests/test_docker_helper.py tests/test_rpc_keys_init.py tests/test_compose_topology.py -q --timeout=240`
Expected: all pass.

Run: `.venv/bin/python -m pytest tests/ -q --timeout=60 --ignore=tests/test_ibrx_async.py`
Expected: green. The subprocess test carries its own 240 s timeout marker. If a test outside these files fails, first check whether it fails on master too before changing anything.

- [ ] **Step 5: Commit**

```bash
git add tests/ai/runtime/child_service.py tests/ai/runtime/test_crash_restart_subprocess.py AGENTS.md docs/OPERATIONAL_STATE.md
git commit -m "test: restart a real ai process after the trader accepted a decision

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

## Self-review

- **Spec coverage.** 4: `RpcClients` (Task 2), `Controller` (9), `SignalIntake` (8), `Submitter` (6), `ReportingOutbox` (7), `ai.duckdb` on `mmr_ai_data` with work off the loop (1, 10). 5.1: epoch held and persisted (3), carried signed on submit, signal and reconcile reads (2), successor reconciles older-epoch commands under its own epoch (6, 11). 5.2: entry slots inside SP1's window (4), position cycles after the cutoff while ARMED/PAUSED with owned positions (9), KILLED/STOPPED (9, Ruling 7), reconciliation independent of cycles and budget (9), missed slots never replayed (9), bounded work and non-blocking model work (9). 5.3: ids derived from source + action identity (5), exact body persisted before send (6), pre-send failure unsent until expiry (6), unknown → reconcile, same id and body on retry (6), expiry and leadership re-checked before each send (6). 5.5 intake: atomic opportunity + cursor, MISSED, coverage gap (8). 7: costs and baselines through the outbox, outage and lost acknowledgement (7, 11). 9: every row of the failure table that involves the runtime (6, 7, 9, 10, 11, 12). 12: crash 1, 3, 4 (11), crash 2 with a real process (12), signed epoch (11), durability (7, 10, 11). 6.7: never publishes on start or restart (2, 11).
- **Names.** Plan 1: `grant_ai_controller_epoch`, `read_ai_signals`, `get_ai_paper_decision`, `controller_epoch=` on `TypedRpcClient.call`, the four epoch codes, `SIGNAL_CURSOR_AHEAD`, `ServedStack.signed(..., controller_epoch=)`. Plan 2: `record_ai_cost` / `record_simulated_decision` request fields and `INSERTED` / `DUPLICATE` / `REFUSED` with `retryable`. Plan 3: `discover_ai_candidates` (90 s, own socket). Plan 4: `Migration`, `AiStore`, `AttemptJournal.acost_events_after`, `CostEvent`, cost kinds, `ModelGateway.start`, `DecisionDeadline`, `ReplayRecorder`, `check_credentials`, `build_gateway`, `config_text`, `FakeClock`.
- **Open for the owner.** Ruling 17 (opt-in compose profile `ai`), Ruling 12 (cost mapping, set by the coordinator), Ruling 6 (slot alignment from the open), Ruling 9 (300 s signal age).
