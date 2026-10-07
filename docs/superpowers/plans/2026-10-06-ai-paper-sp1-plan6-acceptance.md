# AI Paper SP1 — Plan 6: Acceptance Harness and the Real IB Paper Session — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking. Run the tasks in number order; each task ends with the full suite green.

**Goal:** Prove SP1 end to end. A synthetic harness drives the composed system (real `build_command_stack`, real typed RPC, temp DuckDB, a fake broker that injects races, restarts) through the spec's acceptance scenario and its failure variants. `mmr experiment acceptance` runs the same scenario against the real paper stack with the real `ai_research` and `ai_supervisor` keys. A written runbook takes the owner through one real IB paper session: preflight gate, arm, run, flatten, verify, evidence, abort.

**Architecture:** One scenario, two drivers. `trader/acceptance/` holds the scenario as plain code over a small port (`AcceptancePort`: the typed RPC calls it needs) plus the preflight gate, the run journal and the signed report. The synthetic driver serves the real production registry over sockets on top of a composed stack whose broker is the Plan 1 broker simulator (extracted into `tests/sp1_fixtures.py`). The real driver is `mmr experiment acceptance`, which builds two `ServiceIdentity` clients (`ai_research`, `ai_supervisor`) from `~/.config/mmr/keys/rpc/` on the host, plus the operator `cli` client only with `--place-orders` (used only for `acceptance_mark_start` and `acceptance_shrink_probe`). The trader gains one read (`get_acceptance_preflight`) and the CLI gains `mmr flatten` for the abort path.

**Tech Stack:** Python 3.12, pytest, pyzmq typed RPC (Plan 2 `ServiceIdentity`, `TypedRpcClient`, `TypedRpcServer`), DuckDB, Ed25519 report signing (`trader/research/signing.py`). No new dependencies.

**Spec:** `docs/superpowers/specs/2026-10-05-ai-paper-sp1-foundation-design.md` (lands with PR #46; not on master. Immutable source: PR #46 commit `2a021c204798907736b7d3380901c0d7f40e7ac6`. Read it with `git fetch origin 2a021c204798907736b7d3380901c0d7f40e7ac6 && git show 2a021c204798907736b7d3380901c0d7f40e7ac6:docs/superpowers/specs/2026-10-05-ai-paper-sp1-foundation-design.md`, or `gh pr checkout 46`.) Do not copy it: section 6 "Testing" (all of it, and the "Acceptance harness" block), section 7 item 6, section 9 (paper reset question, now answered). Plans 1–5 are binding inputs; owner answers of 2026-10-06 are folded into them.

## Global Constraints

- **Order of merges.** Plans 1–5 and bug fix #47 are on master before Task 1. Line numbers below cite PR #46 commit `2a021c204798907736b7d3380901c0d7f40e7ac6` for Plan 1 code and master `8116f6a5` otherwise; the function name is the anchor.
- **No orders without the owner.** No task, test or script in this plan places a broker order against IB. Only the owner runs the paper session, and `mmr experiment acceptance run` sends nothing unless `--place-orders` **and** `--confirm-account <the paper account id>` are given and the account matches the broker.
- **Real methods, real keys, no seeding** (spec 6): the harness registers its deployment with `register_ai_deployment` signed by `ai_research`, publishes and decides with `ai_supervisor`, and never opens a DuckDB file. A test proves the acceptance package imports no store module.
- **Host only.** `mmr experiment acceptance` refuses to run inside a container (`/.dockerenv` or `/run/.containerenv`, the Plan 2 ruling 15 check). No container gets the `ai_research` or `ai_supervisor` private key for it.
- **Paper only, armed only, flat only.** The harness refuses unless the account is paper, an experiment is `ARMED` and the preflight gate (ruling 3) passes.
- **Pass only on broker evidence.** "Flat" means a broker generation with no position and no working order (Plan 4 K6 / Plan 1 `_poll_flat`), shown by the `equity_daily` row `FLAT` with `open_positions = 0` and by `get_positions` / `get_open_orders` being empty.
- No container restart, broker order or deploy is authorized by this plan; the runbook (Task 8) is for the owner.
- Test-first. Single files: `.venv/bin/python -m pytest <path> -q --timeout=30`. Full suite: `.venv/bin/python -m pytest tests/ -q --timeout=60 --ignore=tests/test_ibrx_async.py`. Every task ends with the full suite green.
- Commit subjects: `feat:` / `fix:` / `test:` / `refactor:` / `docs:`, lowercase, imperative. Every commit ends with a blank line and `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.

## Rulings (spec silent, ambiguous, or decided by the owner)

1. **One scenario, two drivers.** The spec's harness ("real methods, real keys, host, paper, `ARMED`") and the task's composed-system harness are the same scenario code with two ports: `RpcAcceptancePort` over `TypedRpcClient`. In tests the clients talk to a `TypedRpcServer` on a composed stack; on the host they talk to the published loopback ports 42101/42102. Nothing in `trader/acceptance/` knows which.
2. **Identities.** The harness signs with `ai_research` (register) and `ai_supervisor` (publish, decide, and every read it needs: `get_experiment`, `get_positions`, `get_open_orders`, `get_command`, `get_scoreboard`, `get_acceptance_preflight`; all grant `ai_supervisor` after the Plan 3–5 owner answers). It needs no `cli` key for steps 1–7 and the end checks. The only `cli` calls are `acceptance_mark_start` and `acceptance_shrink_probe` (ruling 23), made through a third client that `run --place-orders` loads from `cli.key`; without `--place-orders` it is never loaded. Arming (`mmr experiment start`), `mmr scoreboard verify` (human only), `mmr flatten` and `mmr experiment stop` stay operator `cli` steps in the runbook.
3. **Paper reset (#34, owner decision 2026-10-06): no IB reset by default.** A preflight gate (`mmr experiment acceptance preflight`, Task 5) must pass before `mmr experiment start` and again at the start of a **first** `run`. A resume (`run --run-id`) never runs it: it runs the separate resume validation of ruling 15. It checks: paper account, no position, no working order, no unresolved command (`CommandLedger.reconcilable()` empty, no non-terminal liquidation root, no active exit owner), breaker clear, and a **trustworthy starting equity** (ruling 4). If any check fails, the gate prints `STOP` with the reasons and exits 1; cleanup (`mmr flatten`, cancels, `mmr reconcile`) or an IB paper reset is then an explicit operator decision, after which the gate is run again. Nothing cleans up automatically.
4. **Trustworthy starting equity** means all of: the capture comes from a promoted, fenced broker generation (`capture_risk_snapshot` succeeds, no `GENERATION_STAGING`); `account_mode == "paper"` and the account id starts with `DU`; `net_liquidation` finite and `> 0`; its account-value timestamp at most 300 s old; `DailyPnL` present and finite; base currency `USD`, or a finite positive USD rate (`start_fx_from_cash`, Plan 4 K13); two captures at least 30 s apart differ by at most 0.1 % (a flat account's value must be stable).
5. **Harness sizes.** Deployment conids `[A, B]` (defaults AAPL 265598, MSFT 272093; owner ruling 17), `evidence_order_notional = --notional` (default 2000 USD). Entries carry an explicit `quantity` (defaults A = 3, B = 1) so the bot never uses its 5 % maximum (about 50,000 USD on a 1M paper account). Before each `ENTER` the harness reads `get_snapshot` and refuses `HARNESS_NOTIONAL_TOO_SMALL` when `quantity × ask > notional`. The partial close of A is 1 share (`0 < 1 < 3`, 2 remain).
6. **Honest deployment record.** `strategy_path` = a catalogue file (`strategies/opening_range_breakout.py`, class `OpeningRangeBreakout`); `strategy_digest` = sha256 of that file's bytes (still a claim, Plan 3 R19); `decider = "acceptance_harness"`; `decider_verdict = "DEPLOY"` (Plan 3 admission needs it); `evidence_ref = "acceptance:<run_id>"`; `style = "intraday_long"`. The report says the record is a harness fixture, not research evidence.
7. **Deterministic ids and resume.** `run_id = "acc-" + YYYYMMDD + "-" + 6 hex`. Decision ids are `<run_id>-e-a`, `-e-b`, `-pc-a`, `-c-a` (all match Plan 3 R17); policy `command_id = <run_id>-pol`. `run --run-id` resumes (ruling 15): the journal stores the exact serialized request body before each send and replays those bytes, so a host crash never creates a second command. The run journal (`~/.local/share/mmr/acceptance/<run_id>/journal.jsonl`, append-only, mode 0600) records every request (exact body) and receipt.
8. **Two phases on the real session.** `run` does steps 1–7 and ends with position B open and protected; the session flatten closes B at 15:45 ET (`calendar_policy.py:18`), proven flat by 15:55 (`:19`). `finish` runs after 15:55 ET: it waits up to 20 minutes for the `equity_daily` row and checks the end state. `run` must start between 09:40 and 14:30 ET (entries are admitted 09:35–15:30, `calendar_policy.py:16`, plus margin for fills and the partial close before the AI cutoff cancel, Plan 3 R26).
9. **What a step waits for.** An `ENTER` passes when `get_positions` shows the quantity and `get_open_orders` shows its stop working (120 s timeout, else `ENTRY_NOT_PROTECTED`). A `PARTIAL_CLOSE` passes when `get_command("aip-…")` reports `outcome.liquidation_state == "DONE"` and the broker-order evidence read (ruling 14, never `get_open_orders` or `mmr orders`, which drop the OCA columns) shows a stop and a target for the remainder in one OCA group with `oca_type == 2` and `remaining_quantity` equal to the remainder; the evidence rows are kept in the report. A `CLOSE` passes on `liquidation_state == "CLOSED"`, position zero, no order on that conid. Timeouts never trigger a new command; they fail the run.
10. **Signed report.** `AcceptanceReport` follows `FaultDrillReport` (`scripts/automation_fault_drill.py:781-817`): canonical JSON, Ed25519 signature with `--signing-key` (an operator key the owner keeps; mandatory with `--place-orders`, ruling 20) or, for synthetic tests only, an ephemeral key, `commit_digest` / `config_digest` (`:742-761`). The report lists each step, its evidence and pass/fail; `passed` is true only if every step and every end check passed.
11. **Abort tool.** There is no CLI flatten today (`liquidate_account` has no client; the dashboard has pause, resume and cancel-all only, `web/command_center/routes_commands.py:617,792,802`). Task 6 adds `mmr flatten --reason TEXT [--wait]` over the typed `liquidate_account` (allow-list `HUMAN`; no preflight nonce on paper, `production_api.py:1880-1884`).
12. **What is reused, what is not.** Reused: the broker simulator and composed stack of `tests/test_safe_close_integration.py:50-276,575-579` (moved, Task 1); the signed-report pattern of `scripts/automation_fault_drill.py:781-817,904-953`; the read-only stack smoke `scripts/paper_e2e.sh` with `tests/paper_e2e/test_stack_gate.py` and `test_typed_rpc_queries.py` (orders stay off: `MMR_PAPER_E2E_LIVE_ORDERS` unset, `tests/paper_e2e/conftest.py:246`). Not reused: `scripts/run_paper_soak.py` (its `unhandled_errors` / `unresolved_commands` have no live exporter, docstring :34-42, and it targets the dashboard), `scripts/session_open_check.py` / `session_close_check.py` (their context needs a `paper-v1` artifact digest, `trader/operations/session_checklist.py:75-96`) and `scripts/paper_evidence_report.py` (old-path promotion evidence; it opens the DB file, which the host cannot).

13. **The live OCA shrink proof is mandatory (spec 5.1; reviewers Grok and OpenAI; a former owner question).** A session counts as SP1 acceptance only if the real IB paper session itself proves that a partial fill of one OCA exit shrinks its sibling (`ocaType=2`). Matching `oca_group` and `oca_type` is not proof. Synthetic and fake-broker results never count as live proof: the report field `oca_shrink` is `PROVEN` only with `evidence_source == "ib_paper"`. The proof is a controlled partial exit on a third small position (`S`, AAPL, the same 3 shares, the same notional rule; Task 7): after `S` is entered and protected by the bot's own stop and target in one `ocaType=2` group, the trader-side probe modifies the **target** only: limit at the current bid, `displaySize = 1`, so IB executes it in slices of one share and each slice is a partial fill. Pass evidence is broker evidence only: a target status event with `0 < filled < 3` **and** the sibling stop with `remaining_quantity == 3 - filled` (also `get_positions` equal to that residual) on the same promoted generation, both with `oca_type == 2`, read through the evidence read (ruling 14), including the persisted `status_events` (ruling 14), so a fast sequence is not missed. If a fill and the shrink happen between two polls, the stored events still carry their own quantities; if the events are missing or have a cursor gap, the result is `UNPROVEN`. Outcomes: `PROVEN`; `UNPROVEN` (`OCA_SHRINK_UNPROVEN`: IB gave no observable partial fill, e.g. the whole order filled in one status event, or nothing filled within 60 s); `FAILED` (`OCA_SIBLING_NOT_SHRUNK`: a partial fill was seen and the sibling kept the old quantity; the harness stops and the runbook abort A4 flattens at once). `UNPROVEN` never becomes a pass: the report says `passed = false`, the session is not SP1 acceptance, and the gate stays open for another day. The harness never raises the size, never uses a second symbol or a larger notional, and never engineers an unsafe fill to get a pass. The bracket-children-after-partial-entry question (Plan 1 open point) stays an observation only.
14. **A typed, generation-fenced broker-order evidence read.** `_trade_to_wire` (`trader/messaging/cli_surface.py:85-104`) and SDK `orders()` (`trader/sdk.py:843-914`) drop `oca_group` and `oca_type`, so the scenario cannot check linked re-protection through them. Task 2 adds the query `get_broker_order_evidence` (body `{conid: int | None}`, `extra=forbid`) returning `{"generation_id", "promoted": true, "as_of", "orders": [{"order_id", "perm_id", "conid", "leg": "entry"|"stop"|"take_profit"|"close"|"other", "action", "status", "total_quantity", "filled_quantity", "remaining_quantity", "oca_group", "oca_type", "parent_id", "status_events": [{"cursor", "at", "status", "filled_quantity", "remaining_quantity", "total_quantity"}]}]}` built from the promoted broker generation. `status_events` come from a persisted order-status/fill history, not from `ib_async` `Trade.log` (`TradeLogEntry` has only time, status, message and errorCode) and not from `BrokerOrderRow` (current state only). The trader's broker-order ingest appends one row to `broker_order_events` (trader journal database `trader.journal_db`, migration **81**; columns generation_id, cursor, order_id, perm_id, at, status, filled_quantity, remaining_quantity, total_quantity)` on every `orderStatus` or execution callback, in the same transaction that updates the projected row; `cursor` is a gapless per-generation counter. **Migration versions:** `schema_migrations.py` skips a version already recorded, so every Plan 6 table needs a free version. Taken: 1–11, 20–25, 30–34, 40–45, 50–53, Plan 1 35–38, Plan 3 54–56, Plan 5 60–64 (range 60–69), Plan 4 70 (range 70–79). Plan 6 reserves **80–89** and uses 80 (`acceptance_marks`) and 81 (`broker_order_events`); Task 7 adds both to the `schema_migrations.py` docstring and tests that `applied_versions()` contains 80 and 81 on a fresh and on an already-migrated journal. The read returns the rows of the promoted generation only. A gap in `cursor`, or a generation change between two readings, makes every transition across it unobservable. Rule: a transition that was not recorded as an event is UNPROVEN. A passing pair is never built from current rows plus a historical status string, and never from rows of two generations. A generation still staging returns `{"capture_error": "GENERATION_STAGING"}`, never partial data. ACL `{"cli", "dashboard", "ai_supervisor"}`. The scenario asserts through it and the report keeps every row it used. `mmr orders` and `get_open_orders` stay as they are.
15. **Resume is not first run.** The first-run preflight demands a flat account; after `ENTER A` the account correctly holds A and its stop. `run --run-id` therefore skips it and runs `resume.validate_resume(journal, port)`: the account may hold only the positions and orders the journal says were sent (by conid and leg, read through ruling 14), nothing else, no unresolved command other than the journal's in-flight ones, breaker clear, experiment `ARMED`. Exact bodies: before each send the journal writes an `intent` record with the serialized body (canonical JSON text, including `expires_at`), and a `receipt` record after the reply. Resume never recomputes a body: `canonical_request_hash` includes the body (`command_coordinator.py:493-510`), so a new `expires_at` under the same command id is rejected. For each `intent` without a `receipt` it first asks `get_command(<command_id>)`; a found command becomes the receipt; if there is none it replays the stored bytes unchanged; if the stored `expires_at` has passed (`DECISION_EXPIRED`) the step fails `RESUME_EXPIRED_NO_COMMAND` and no new decision is built. A journal whose last completed step cannot be reconciled with the broker fails `RESUME_STATE_MISMATCH`.
16. **Per-trip evidence through a typed read.** The end checks assert the A, B and S trips (conid, closed quantity, state) with Plan 5's `get_experiment_trips` (Plan 5 ruling 19), not with the aggregate `get_scoreboard` and never from a host DB. `ai_supervisor` may read it.
17. **Owner ruling: sizes.** AAPL 3 shares and MSFT 1 share with a 2,000 USD notional are the paper defaults. On the day the harness resolves and verifies both conids (`resolve`), and for each `ENTER` refuses `HARNESS_NOTIONAL_TOO_SMALL` when `quantity × ask` exceeds the notional. It never raises the notional to make an entry pass.
18. **Owner ruling: Telegram.** Stays off unless the owner has supplied a bot and chat id. Until then the synthetic tests own the outbox; the report says `telegram_live_delivery: "UNTESTED"` when it is off.
19. **Owner ruling: live restart.** A live trader restart while B is open is optional and not part of the runbook gate. The synthetic restart tests ([4]) are the gate. The report carries `live_restart_recovery: "NOT_PROVEN"` and no text anywhere may claim that live restart recovery was proven; an owner who runs the optional restart records it as a separate note, not as a pass criterion.
20. **Owner ruling: signing key.** The real-session report is signed with an operator key the owner keeps (`--signing-key`, Ed25519 PEM) and checked later with the matching public key. `run --place-orders` and `finish` refuse to start without `--signing-key` (`SIGNING_KEY_REQUIRED`). Ephemeral keys exist for synthetic tests only; the report records `signing_key: "operator" | "ephemeral"`, and a real-session report with `"ephemeral"` is invalid.
21. **Owner ruling: one session.** One passing session meets spec 6 if it includes the shrink proof (ruling 13) and every other step and end check passes. A failed or incomplete session, including `oca_shrink` `UNPROVEN`, is not a pass. No second day is required after a pass.
22. **Owner ruling: harness fixture.** The report keeps saying the deployment is a harness fixture (`deployment_record: "harness_fixture"`, `strategy_digest` still a claim until SP2, Plan 3 R19).
23. **The probe is bound to a durable acceptance mark, keeps the OCA link, and uses recorded events (round 2 reviews by Grok and OpenAI).** (a) Only `acceptance_mark_start` (cli, from a first `acceptance run --place-orders`) writes the mark: exact experiment id, run id, conid and entry decision id `<run_id>-e-s`, 24 h life, one probe. `acceptance_shrink_probe` must name all four and refuses `PROBE_MARK_MISSING`, `PROBE_MARK_MISMATCH` or `PROBE_MARK_STALE`; the off-by-default flag and the `cli` ACL are not the scope. (b) The modify copies the broker-confirmed working target `Order` and changes only `lmtPrice` and `displaySize`; `orderId`, `ocaGroup`, `ocaType`, `totalQuantity`, `action` and `orderType` are kept, because `ib_async` replaces the whole order. After the modify the trader re-reads the broker and refuses `PROBE_OCA_LOST` (no wait) if `oca_type != 2` or the group is gone. A whole fill in one event stays `UNPROVEN`. (c) Shrink evidence comes only from the persisted `broker_order_events` history (ruling 14), correlated by generation and cursor; unobservable transitions are `UNPROVEN`. (e) S has its own entry builder with a target, and the probe is enabled only after broker evidence shows both OCA legs working (Task 7). (f) After the proof, whatever its result, S's residual is closed through the safe close and the run phase ends only with S flat and no working orders; the harness never waits for the target to finish. (d) `ai_paper.acceptance_probe` is a typed Plan 3 Task 3 field (default `false`), so setting it does not stop `trader_service`.

## Review Focus

1. **The host process dies between a decision and its receipt.** Expect `run --run-id` to replay the same command id and never create a second order. → Task 3 `test_resume_after_a_crash_replays_and_sends_no_second_order`.
2. **The account is not clean when the owner starts** (a leftover position, a working order from an old session, an `OUTCOME_UNKNOWN` command). Expect `STOP` with the exact reasons and no write. → Task 2 `test_preflight_stops_on_each_unclean_condition`.
3. **A flatten that never proves flat** (a child submitted but not visible at the deadline). Expect `finish` to report `FAILED_SAFE` and fail, never a pass from "no positions in the cache". → Task 4 `test_invisible_child_at_the_flatten_deadline_fails_the_run`.
4. **Wrong account or wrong host.** `--confirm-account` different from the broker account, or run inside a container. Expect refusal before any call that writes. → Task 5 `test_run_refuses_wrong_account_and_container`.
5. **An entry that does not fill** (thin quote, marketable limit not reached). Expect `ENTRY_NOT_PROTECTED` after 120 s, no second entry, and the AI cutoff cancel (Plan 3 R26) or the flatten cleans up. → Task 3 `test_unfilled_entry_fails_the_step_without_a_retry`.

---

## Spec section 6 → owner map

"Unit" names the plan whose own tests own the item. "Harness" is a Plan 6 composed test (Task in brackets). "Session" is a step of the real paper runbook (Task 8, phase number). **NO OWNER** would mark an item nothing proves; none remains (owner rulings 13–22).

| Spec 6 item | Unit | Harness | Session |
|---|---|---|---|
| Time exit leaves the stop live (repro) | Plan 1 | — | — |
| State machine: lost ack, partial fill, cancel rejected, re-protect failure, missed deadline | Plan 1 | missed deadline, PendingCancel never lands [4] | — |
| Partial close of one of two protected positions; other untouched; breaker clear | Plan 1 | scenario [3] | P4 steps 3–5 |
| Unrequested stop cancel starts the emergency path | Plan 1 | — | — |
| Routine scoped-close progress does not trip the breaker | Plan 1 | scenario end check [3] | P5 breaker check |
| Time exit + AI close on one conid: one root | Plan 1, Plan 3 | [4] | — |
| Time exit during a partial close upgrades to zero | Plan 1 | — | — |
| Account flatten during a partial close: superseded, children reconciled | Plan 1 | — | — |
| Kill during `REPROTECTING` with working replacement exits → `FLAT` | Plan 1, Plan 4 | [4] | not run (needs a 20 % loss; synthetic only by design) |
| Submitted child not yet visible blocks the reduce; `FAILED_SAFE`, no second order | Plan 1 | [4] | — |
| Full close during a flatten joins it | Plan 1 | — | — |
| Old `FAILED_SAFE` root does not block `rescan()` | Plan 1 | — | — |
| Caller never accepts another root's receipt | Plan 1 | — | — |
| Exit OCA: sibling fills first; recovery places only the missing one; zero cancels residual; `DONE` needs both working | Plan 1 | restart between `REPROTECTING` and `DONE` [4] | P4 step 4: both legs working, same OCA group, `oca_type` recorded |
| IB `ocaType=2` shrinks the sibling on a partial fill (spec 5.1 "proves it … in the real paper session") | Plan 1 (fake only) | the probe logic only, never counted as proof [7] | P4S (Task 7, ruling 13): mandatory; `PROVEN` only from broker evidence, else `UNPROVEN`/`FAILED` and no acceptance |
| IB shrinks bracket children after a partial entry fill and cancel (Plan 1 open point) | Plan 3 R26 (fake) | [4] | observe only if it happens, recorded; no forced case by design (ruling 13) |
| Production composition through `command_stack` | Plan 1 | every harness test [1–4] | the real stack |
| Scoreboard: projection matches stored rows; partial exits one trip; `verify` detects edits; late commission; non-USD FX; benchmark immutable | Plan 5 | trips and `verify` after the scenario [3] | P6 `mmr scoreboard verify`, trips |
| Outbox sends once per event id, retries after an outage | Plan 5 | — | synthetic tests own it; live delivery `UNTESTED` unless the owner supplied a bot and chat id (ruling 18) |
| Identities: every trust-matrix edge, forwarded call as `trader`, no container mounts another private key, wrong key, outside allow-list, method without entry, source from key, AI cannot trade | Plan 2 (incl. fullstack test) | every harness call is a signed round trip [1, 3] | P1 `fullstack-tests`, `paper_e2e` smoke |
| `ai_paper`: parity, ceiling, config, drawdown guard, policy timing, tighten before dispatch, attested notional, old-path regression, reductions, idempotency, coordinator checks, deployment seal | Plan 3 (+ #47) | idempotent replay [3], close while `PAUSED` [4] | P4 the real admission path |
| Arming: disabled, leftover position or order, old shared key; stop not flat; `KILLED` → `STOPPED`; lock; AI cannot resume | Plan 4 | — | P2 preflight gate + `experiment start` |
| `equity_daily` after `KILLED` and after `FAILED_SAFE` | Plan 5, Plan 4 | [4] | — |
| `KILLED` stored before the first flatten order; flat only on broker evidence; kill survives restart; both bases | Plan 4 | kill during `REPROTECTING` [4] | — |
| Real IB paper session passes (spec 6) | — | — | P0–P6 |
| Acceptance harness: register via `ai_research`, policy and decisions via `ai_supervisor`, two entries, partial then full close of the first, second left for the session flatten, passes only on broker-evidenced `FLAT`, real keys, no seeding, host only, paper and `ARMED` only, no autonomous loop | — | [2, 3, 5] | P3–P6 |

---

## File map

| File | Responsibility | Task |
|---|---|---|
| `tests/sp1_fixtures.py` (new), `tests/test_safe_close_integration.py` | Broker simulator with race hooks, composed stack, served stack, restart | 1 |
| `trader/acceptance/__init__.py`, `ports.py`, `scenario.py`, `journal.py`, `report.py`, `preflight.py`, `resume.py` (new); `trader/messaging/cli_surface.py`, `trader/messaging/production_api.py`, `trader/messaging/principals.py` | Scenario, port, run journal with exact request bodies, signed report, preflight gate and its `get_acceptance_preflight` query, resume validation, the typed broker-order evidence read `get_broker_order_evidence` | 2 |
| `tests/sp1_acceptance/test_scenario_unit.py`, `tests/sp1_acceptance/test_preflight.py` (new) | Scenario against a fake port; the gate | 2 |
| `tests/sp1_acceptance/test_acceptance_run.py` (new) | Scenario through the composed stack | 3 |
| `tests/sp1_acceptance/test_races.py` (new) | Race and restart variants | 4 |
| `trader/sdk.py`, `trader/mmr_cli.py` | `mmr experiment acceptance preflight|run|finish|status` | 5 |
| `trader/sdk.py`, `trader/mmr_cli.py` | `mmr flatten` | 6 |
| `trader/acceptance/shrink_proof.py` (new), `trader/messaging/production_api.py`, `trader/messaging/principals.py`, `trader/trading/acceptance_probe.py` (new), `tests/sp1_acceptance/test_shrink_proof.py` (new) | The live `ocaType=2` shrink proof step and its trader-side probe command | 7 |
| `docs/PAPER_ACCEPTANCE_SP1.md` (new), `docs/OPERATIONAL_STATE.md`, `AGENTS.md`, `tests/test_acceptance_runbook.py` (new) | Runbook and its parse test | 8 |

---

### Task 1: Shared composed-system fixtures with race hooks

**Files:**
- Create: `tests/sp1_fixtures.py`
- Modify: `tests/test_safe_close_integration.py` (import the moved classes; no test body changes)
- Test: `tests/sp1_acceptance/__init__.py`, `tests/sp1_acceptance/test_fixtures.py`

**Interfaces:**
- Consumes: `tests/test_safe_close_integration.py:50-276` (`_LoopThread`, `_Ingest`, `_BrokerSim`, `_Universe`, `_Composed`, `_enable_automation`) and `:575-579` (`_restart`); Plan 2 `tests/rpc_identity_fixtures.py` (`make_identities`, `write_keyset`); Plan 3 `AiPaperConfig`; Plan 4 `ExperimentServices`; `build_production_registry` (`production_api.py`), `TypedRpcServer`, `TypedRpcClient`.
- Absorbs Plan 3 Task 10's `composed_ai` fixture and helpers from `tests/test_safe_close_integration.py` (one composed stack, not two): `Composed(ai_paper=True)` is that fixture.
- Produces (in `tests/sp1_fixtures.py`):
  - `LoopThread`, `Ingest`, `BrokerSim`, `Universe`, `Composed(tmp_path, loop_thread, clock, *, automation=False, ai_paper=False, sim=None, kill_pct=None)`, `restart(composed, tmp_path) -> Composed` — the moved classes, public names, behaviour unchanged.
  - `BrokerSim` race hooks: `fill_after_cancel(entity, quantity)` (the next `promote()` shows the cancelled order `Filled` instead), `pending_cancel(entity, *, lands=True)` (status `PendingCancel` until a later `promote()`, or for ever), `hide(entity)` / `reveal(entity)` (child not yet visible), `reconnect()` (sets `Ingest.ready = False`, then the next `promote()` opens a new generation and re-enumerates every order, the IB reconnect shape), `auto_fill(order_types=("LMT", "MKT"))` (marketable entries and reduces fill at the next `promote()` at the quote), `quote(conid, bid, ask)`.
  - `ServedStack(composed, identities)` with `client(principal, role="command"|"query") -> TypedRpcClient`, `call(principal, method, body) -> dict`, `restart() -> ServedStack` (new `Composed` on the same journal and the same `BrokerSim`, new servers on new ports), `advance(seconds)`, `run_session(at)`, `tick()`.
  - Fixtures `served` (paper, `ai_paper` enabled, an `ARMED` experiment started through `start_experiment` as `cli`, Telegram off) and `served_with_kill` (the same with `experiment_kill_drawdown_pct = 20` and Telegram on through Plan 5's `build_telegram` with a fake `post`, so the kill alert reaches the outbox).

- [ ] **Step 1: Write the failing tests**

```python
# tests/sp1_acceptance/test_fixtures.py
def test_served_stack_answers_signed_reads_and_writes(served):
    assert served.call("cli", "get_status", {})["account_mode"] == "paper"
    rev = served.call("ai_supervisor", "publish_ai_risk_policy",
                      {"command_id": "pol-1", "limits": PAPER_LIMITS.to_json(), "reason": "r"})
    assert rev["outcome"]["revision"] == 1
    assert served.call("ai_supervisor", "get_experiment", {})["experiment"]["state"] == "ARMED"

def test_pending_cancel_that_never_lands_stays_pending_across_generations(composed_sim):
    composed_sim.add_order("og-x:stop", "og-x", "stop", "SELL", "STP", 3)
    composed_sim.pending_cancel("og-x:stop", lands=False)
    for _ in range(3):
        composed_sim.promote()
    assert composed_sim.orders["og-x:stop"].status == "PendingCancel"

def test_fill_after_cancel_turns_the_cancel_into_a_fill(composed_sim): ...
def test_hidden_child_is_absent_from_the_generation_until_revealed(composed_sim): ...
def test_reconnect_opens_a_new_generation_only_after_staging(composed_sim): ...     # capture raises GENERATION_STAGING first
def test_restart_keeps_the_journal_and_the_broker(served):
    served.call("ai_supervisor", "publish_ai_risk_policy", {"command_id": "pol-1", "limits": PAPER_LIMITS.to_json(), "reason": "r"})
    again = served.restart()
    assert again.call("ai_supervisor", "get_ai_risk_policy", {})["latest_published_revision"] == 1
```

- [ ] **Step 2: Run, expect FAIL** (`ModuleNotFoundError: tests.sp1_fixtures`).
- [ ] **Step 3: Implement.** Move the classes verbatim; `tests/test_safe_close_integration.py` keeps private aliases (`_BrokerSim = BrokerSim`, …) so its bodies stay untouched. `Composed(ai_paper=True)` sets `trader.ai_paper_config = AiPaperConfig(enabled=True, experiment_kill_drawdown_pct=kill_pct)` and `trader.rpc_identity = identities["trader"]` before `build_command_stack`. `ServedStack` builds `build_production_registry(...)` exactly as `trading_runtime.py` does after Plan 2 (`acl=TRADER_ACL`), binds query and command `TypedRpcServer`s on free loopback ports with `identities["trader"]`, and attaches the Plan 4 identity check. Race hooks only change what `promote()` writes; they never call the trader.
- [ ] **Step 4: Run** the new file and `tests/test_safe_close_integration.py`, then the full suite.
- [ ] **Step 5: Commit** — `test: share the sp1 composed stack and add broker race hooks`.

---

### Task 2: The acceptance scenario, its port, the preflight gate, run journal and signed report

**Files:**
- Create: `trader/acceptance/__init__.py`, `trader/acceptance/ports.py`, `trader/acceptance/scenario.py`, `trader/acceptance/journal.py`, `trader/acceptance/report.py`, `trader/acceptance/preflight.py`
- Modify: `trader/messaging/production_api.py` (query `get_acceptance_preflight`, registered next to `get_experiment`), `trader/messaging/principals.py` (`("query", "get_acceptance_preflight"): frozenset({"cli", "dashboard", "ai_supervisor"})`, an explicit set per Plan 3 R23)
- Test: `tests/sp1_acceptance/test_scenario_unit.py`, `tests/sp1_acceptance/test_preflight.py`

**Interfaces:**
- Consumes: `capture_risk_snapshot` via the stack's `broker_snapshot` port, `CommandLedger.reconcilable()` (`command_coordinator.py:717`), `LiquidationRunStore.roots_to_advance_in_tx`, `ExitOwnerRegistry.account_owner`, the breaker store, Plan 4 `start_fx_from_cash` (for the query); Plan 3 wire shapes (`register_ai_deployment {deployment}`, `publish_ai_risk_policy {command_id, limits, reason}`, `submit_ai_paper_decision` with the 12 fields, R17 id rules), Plan 1 `CloseResolution.outcome` keys (`liquidation_state`, `close_root_id`, `remaining_quantity`; `liquidation_service.py:602-610`), Plan 5 report shape; `AttestationSigner`, `canonical_json_bytes`.
- Produces:
  - `ports.AcceptancePort` protocol: `research(method, body) -> dict` (signs as `ai_research`), `supervisor(method, body) -> dict` (signs as `ai_supervisor`), `operator(method, body) -> dict` (signs as `cli`; raises `OperatorChannelUnavailable` when the port was built without it, i.e. a run without `--place-orders`; used only for `acceptance_mark_start` and `acceptance_shrink_probe`), `now() -> datetime`, `sleep(seconds)`. `ports.RpcAcceptancePort(research_client, supervisor_command, supervisor_query, operator_client=None, now=..., sleep=time.sleep)`.
  - `scenario.AcceptanceSettings(run_id, account_id, conid_a, conid_b, quantity_a=3, quantity_b=1, partial_quantity=1, notional=2000.0, strategy_path, strategy_class, strategy_bytes: bytes, step_timeout=120.0)`; `new_run_id(now) -> str`; `decision_id(run_id, step) -> str`.
  - `scenario.AcceptanceScenario(port, settings, journal)` with `run() -> list[StepResult]` (steps 1–7, ruling 8) and `finish() -> list[StepResult]` (end checks). `StepResult(name: str, passed: bool, code: Optional[str], evidence: dict)`. Steps: `preflight`, `register`, `publish`, `enter_a`, `enter_b`, `partial_close_a`, `close_a`, then Task 7's `enter_s`, `shrink_proof` and `settle_s` (the run is `PASS` only if `oca_shrink` is `PROVEN` on the real session **and** `settle_s` passed: on every path, PROVEN included, the run phase ends with S flat and no working order for it; `settle_s` runs after `shrink_proof` and before the run can pass); end checks: `session_flat`, `equity_row_flat`, `no_positions_or_orders`, `round_trips` (A, B and S through `get_experiment_trips`: conid, closed quantity, state; ruling 16), `oca_shrink`, `no_incidents`.
  - `journal.RunJournal(directory: Path)`: `append(kind, payload)`, `entries() -> list[dict]`; file `journal.jsonl` mode 0600 in a 0700 directory; refuses to open a symlink.
  - `report.AcceptanceReport` (fields of `FaultDrillReport` minus coverage, plus `run_id`, `account_id`, `steps`, `end_checks`, `harness_note`), `sign(signer, key_source)`, `verify(public_key)`, `write(path)`.

  - Query `get_acceptance_preflight` → `{"account_id", "account_mode", "generation_id", "net_liquidation", "nlv_as_of", "daily_pnl", "base_currency", "usd_per_base", "positions": [...], "working_orders": [...], "unresolved_commands": [ids], "open_liquidation_roots": [ids], "exit_owner": str | None, "breaker_tripped": bool, "experiment_state": str | None}`; any capture error is returned as `{"capture_error": code}`, never raised.
  - `preflight.evaluate_preflight(first: dict, second: dict, *, now) -> PreflightResult(passed: bool, failures: tuple[str, ...])` (pure; ruling 3 and 4 codes: `NOT_PAPER`, `CAPTURE_UNAVAILABLE`, `POSITIONS_OPEN`, `WORKING_ORDERS`, `UNRESOLVED_COMMANDS`, `LIQUIDATION_OPEN`, `EXIT_OWNER_ACTIVE`, `BREAKER_TRIPPED`, `EQUITY_INVALID`, `EQUITY_STALE`, `DAILY_PNL_UNKNOWN`, `FX_UNAVAILABLE`, `EQUITY_UNSTABLE`).

  - Query `get_broker_order_evidence` (ruling 14): `cli_surface.broker_order_evidence_view(generation, trades, conid)` plus registration in `production_api.py` and ACL `("query","get_broker_order_evidence"): frozenset({"cli", "dashboard", "ai_supervisor"})`. `AcceptancePort.evidence(conid=None) -> dict`.
  - `AcceptancePort.trips(experiment_id) -> dict` over Plan 5 `get_experiment_trips`.
  - `journal.RunJournal` record kinds `intent` (step, method, principal, `body_json` exact text, `command_id`), `receipt` (step, reply), `step` (result). `resume.validate_resume(journal, port) -> PreflightResult` (ruling 15; codes `RESUME_STATE_MISMATCH`, `RESUME_UNKNOWN_POSITION`, `RESUME_UNKNOWN_ORDER`, `RESUME_EXPIRED_NO_COMMAND`).
  - `AcceptanceReport` extra fields: `oca_shrink` (`PROVEN|UNPROVEN|FAILED|NOT_RUN`), `evidence_source` (`ib_paper|synthetic`), `live_restart_recovery: "NOT_PROVEN"`, `telegram_live_delivery`, `deployment_record: "harness_fixture"`, `signing_key`, `order_evidence` (the rows used).

- [ ] **Step 1: Write the failing tests** (`FakePort` records calls and returns scripted receipts; a fake clock whose `sleep` advances it)

```python
def test_run_sends_the_spec_sequence_with_the_right_principals(fake_port, settings, journal):
    AcceptanceScenario(fake_port, settings, journal).run()
    assert fake_port.calls == [
        ("supervisor", "get_acceptance_preflight"), ("supervisor", "get_acceptance_preflight"),   # two reads 30 s apart
        ("supervisor", "get_experiment"),
        ("research", "register_ai_deployment"), ("supervisor", "publish_ai_risk_policy"),
        ("supervisor", "get_snapshot"), ("supervisor", "submit_ai_paper_decision"),   # ENTER A
        ("supervisor", "get_snapshot"), ("supervisor", "submit_ai_paper_decision"),   # ENTER B
        ("supervisor", "submit_ai_paper_decision"),                                   # PARTIAL_CLOSE A
        ("supervisor", "submit_ai_paper_decision"),                                   # CLOSE A
        ("supervisor", "get_snapshot"), ("supervisor", "submit_ai_paper_decision"),   # enter_s: ENTER S, own builder with a target (Task 7)
        ("operator", "acceptance_mark_start"),                                        # only after both S legs are working on the evidence read
        ("operator", "acceptance_shrink_probe"),                                      # shrink_proof (reads filtered out)
        ("supervisor", "submit_ai_paper_decision")]                                   # settle_s: CLOSE S
    # reads used for waiting (get_positions, get_open_orders, get_command, get_broker_order_evidence) are filtered out above.
    # Task 2 defines the step list and the port; Task 7 implements enter_s, shrink_proof and settle_s behind it.
    # With a port built without the operator channel (a run without --place-orders) the scenario stops at the preflight and plan print; it never reaches the operator calls.

def test_decision_ids_are_deterministic_and_valid(settings):
    ids = [decision_id(settings.run_id, s) for s in ("e-a", "e-b", "pc-a", "c-a")]
    assert all(re.fullmatch(r"[A-Za-z0-9_-]{8,64}", i) for i in ids) and len(set(ids)) == 4

def test_deployment_record_is_honest(fake_port, settings, journal):
    AcceptanceScenario(fake_port, settings, journal).run()
    dep = fake_port.body_of("register_ai_deployment")["deployment"]
    assert dep["strategy_digest"] == "sha256:" + hashlib.sha256(settings.strategy_bytes).hexdigest()
    assert (dep["decider"], dep["decider_verdict"], dep["evidence_ref"]) == (
        "acceptance_harness", "DEPLOY", f"acceptance:{settings.run_id}")
    assert sorted(dep["conids"]) == sorted([settings.conid_a, settings.conid_b])

@pytest.mark.parametrize("state", [None, "PAUSED", "KILLED", "STOPPED"])
def test_refuses_unless_an_experiment_is_armed(fake_port, settings, journal, state):
    fake_port.experiment_state = state
    results = AcceptanceScenario(fake_port, settings, journal).run()
    assert (results[-1].name, results[-1].code) == ("preflight", "EXPERIMENT_NOT_ARMED")
    assert not fake_port.writes()

def test_notional_too_small_refuses_before_the_entry(fake_port, settings, journal):
    fake_port.ask = 1_000.0                                       # 3 x 1000 > 2000
    results = AcceptanceScenario(fake_port, settings, journal).run()
    assert results[-1].code == "HARNESS_NOTIONAL_TOO_SMALL" and "submit_ai_paper_decision" not in fake_port.methods()

def test_a_timeout_fails_the_step_and_sends_nothing_more(fake_port, settings, journal):
    fake_port.never_fill("e-a")
    results = AcceptanceScenario(fake_port, settings, journal).run()
    assert results[-1].code == "ENTRY_NOT_PROTECTED"
    assert fake_port.methods().count("submit_ai_paper_decision") == 1

def test_the_evidence_read_carries_oca_fields_and_is_generation_fenced(served):                # ruling 14
    rows = served.call("ai_supervisor", "get_broker_order_evidence", {"conid": AAPL})["orders"]
    assert {"oca_group", "oca_type", "remaining_quantity", "leg", "status_events"} <= set(rows[0])
    served.sim.stage_generation(); assert served.call("ai_supervisor", "get_broker_order_evidence", {})["capture_error"] == "GENERATION_STAGING"

def test_the_old_orders_read_still_drops_oca_so_the_scenario_must_not_use_it(fake_port):          # guards ruling 14
    assert "get_open_orders" not in fake_port.methods_used_for_assertions()

def test_resume_validation_accepts_the_journals_own_position_and_stop_and_refuses_extras(fake_port, settings, journal):  # ruling 15
    journal.record_receipt("enter_a"); fake_port.hold(A=3, stop=True)
    assert validate_resume(journal, fake_port).passed
    fake_port.hold(extra_position=MSFT)
    assert "RESUME_UNKNOWN_POSITION" in validate_resume(journal, fake_port).failures

def test_the_intent_record_holds_the_exact_body_and_resume_replays_those_bytes(fake_port, settings, journal):   # ruling 15
    AcceptanceScenario(fake_port, settings, journal).run_until("enter_a", crash_before_receipt=True)
    stored = journal.entries()[-1]["body_json"]
    AcceptanceScenario(fake_port, settings, journal, now=lambda: later(minutes=5)).resume()
    assert fake_port.sent_bodies("submit_ai_paper_decision")[0] == stored            # same bytes, same expires_at

def test_resume_with_an_expired_body_and_no_command_fails_without_a_new_decision(fake_port, settings, journal):
    AcceptanceScenario(fake_port, settings, journal).run_until("enter_a", crash_before_receipt=True); fake_port.forget_commands()
    r = AcceptanceScenario(fake_port, settings, journal, now=lambda: later(minutes=30)).resume()
    assert r[-1].code == "RESUME_EXPIRED_NO_COMMAND" and fake_port.methods().count("submit_ai_paper_decision") == 1

def test_the_report_flags_a_fixture_untested_restart_and_the_signing_key_kind(...):               # rulings 18-22
    report = build_report(...); assert (report.deployment_record, report.live_restart_recovery) == ("harness_fixture", "NOT_PROVEN")

def test_partial_close_needs_done_and_both_legs_in_one_oca_group(fake_port, settings, journal):
    fake_port.after_partial(liquidation_state="DONE", legs=[("stop", 2, "g1"), ("take_profit", 2, "g2")])
    assert AcceptanceScenario(fake_port, settings, journal).run()[-1].code == "REPROTECT_NOT_LINKED"

def test_finish_passes_only_on_a_flat_equity_row_and_empty_broker(fake_port, settings, journal):
    fake_port.equity_row = {"session_end_state": "FAILED_SAFE", "open_positions": 1}
    assert not all(r.passed for r in AcceptanceScenario(fake_port, settings, journal).finish())

def test_journal_is_append_only_and_private(tmp_path):
    j = RunJournal(tmp_path / "acc-1"); j.append("step", {"a": 1})
    assert stat.S_IMODE(os.stat(tmp_path / "acc-1" / "journal.jsonl").st_mode) == 0o600

def test_report_signature_round_trip_and_tamper(tmp_path): ...       # as FaultDrillReport.verify

def test_acceptance_package_never_imports_a_store():
    banned = ("duckdb", "trader.data", "trader.scoreboard.store", "trader.automation.ai_deployments")
    for path in Path("trader/acceptance").glob("*.py"):
        tree = ast.parse(path.read_text())
        names = {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names} | \
                {n.module or "" for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
        assert not [m for m in names if m.startswith(banned)], path

# test_preflight.py — evaluate_preflight is pure; the query runs on Task 1's served stack
CLEAN = {"account_id": "DU111111", "account_mode": "paper", "generation_id": 7, "net_liquidation": 1_000_000.0,
         "nlv_as_of": iso(NOW - dt.timedelta(seconds=40)), "daily_pnl": 0.0, "base_currency": "USD", "usd_per_base": 1.0,
         "positions": [], "working_orders": [], "unresolved_commands": [], "open_liquidation_roots": [],
         "exit_owner": None, "breaker_tripped": False, "experiment_state": None}

def test_clean_account_passes():
    assert evaluate_preflight(CLEAN, {**CLEAN, "net_liquidation": 1_000_200.0}, now=NOW).passed

@pytest.mark.parametrize("change,code", [
    ({"account_mode": "live"}, "NOT_PAPER"), ({"account_id": "U123"}, "NOT_PAPER"),
    ({"capture_error": "GENERATION_STAGING"}, "CAPTURE_UNAVAILABLE"),
    ({"positions": [{"conid": 265598, "quantity": 1.0}]}, "POSITIONS_OPEN"),
    ({"working_orders": [{"order_entity_id": "x", "is_external": True}]}, "WORKING_ORDERS"),
    ({"unresolved_commands": ["aip-old"]}, "UNRESOLVED_COMMANDS"), ({"open_liquidation_roots": ["r"]}, "LIQUIDATION_OPEN"),
    ({"exit_owner": "r"}, "EXIT_OWNER_ACTIVE"), ({"breaker_tripped": True}, "BREAKER_TRIPPED"),
    ({"net_liquidation": math.nan}, "EQUITY_INVALID"), ({"net_liquidation": 0.0}, "EQUITY_INVALID"),
    ({"nlv_as_of": iso(NOW - dt.timedelta(seconds=301))}, "EQUITY_STALE"), ({"daily_pnl": None}, "DAILY_PNL_UNKNOWN"),
    ({"base_currency": "EUR", "usd_per_base": None}, "FX_UNAVAILABLE")])
def test_preflight_stops_on_each_unclean_condition(change, code):                     # Review Focus 2
    result = evaluate_preflight({**CLEAN, **change}, {**CLEAN, **change}, now=NOW)
    assert not result.passed and code in result.failures

def test_unstable_equity_stops():
    assert "EQUITY_UNSTABLE" in evaluate_preflight(CLEAN, {**CLEAN, "net_liquidation": 1_002_000.0}, now=NOW).failures

def test_query_reports_an_unresolved_command_and_a_working_order(served): ...
def test_query_acl_is_exact(): assert TRADER_ACL[("query", "get_acceptance_preflight")] == frozenset({"cli", "dashboard", "ai_supervisor"})
```

- [ ] **Step 2: Run, expect FAIL.**
- [ ] **Step 3: Implement.** The query handler reads only trader-owned state on the liquidation worker (as `get_experiment` does) and returns a capture error as `{"capture_error": code}`, never raising. `evaluate_preflight` is pure. The scenario's `preflight` step reads the query twice, `sleep(30)` apart, and passes only on `evaluate_preflight(...).passed` and an `ARMED` experiment (`get_experiment`). One private method per step, each returning a `StepResult` and appending request and receipt to the journal before returning; `run` stops at the first failed step, with one narrow exception: when `shrink_proof` returns `OCA_SHRINK_UNPROVEN`, `settle_s` always runs (the CLOSE of S's residual) before `run` returns failure, and nothing else runs after it (no new entry, no retry). `OCA_SIBLING_NOT_SHRUNK` is not an exception: `run` stops at once and the runbook abort A4 flattens. Policy published is `PAPER_LIMITS` (the owner ceiling caps it anyway). Decisions: `expires_at = now + 10 min`; `ENTER` for A and B carries `stop_price = round(ask × 0.98, 2)`, no target (S has its own builder, Task 7); `PARTIAL_CLOSE` carries `quantity = partial_quantity`, `stop_price` = A's original stop, `target_price = round(ask × 1.02, 2)`; `CLOSE` carries no quantity; reductions send `deployment_digest = null`, `policy_revision = null` (Plan 3 R16). Waiting polls every 2 s with the port's `sleep`. `finish`: poll `get_scoreboard` until today's `equity_daily` row exists (20 min), then the end checks of ruling 9 and the trips check (A: one closed trip with `exit_qty == quantity_a`; B: one closed trip). Every receipt with `state == "REJECTED"` fails its step with the receipt's `error_code`. A partial close that ends `CLOSED` (an exit filled first, Plan 1) fails its step with `PARTIAL_ENDED_CLOSED`; it is not a safety failure, but the scenario did not run as written.
- [ ] **Step 4: Run, expect PASS**, then the full suite.
- [ ] **Step 5: Commit** — `feat: add the sp1 acceptance scenario, preflight gate and signed report`.

---

### Task 3: The scenario through the composed system

**Files:**
- Create: `tests/sp1_acceptance/test_acceptance_run.py`
- Test: that file

**Interfaces:**
- Consumes: Task 1 `served`, `BrokerSim.auto_fill`, `quote`; Task 2 `AcceptanceScenario`, `RpcAcceptancePort`.
- Produces: helper `drive_session_to_flat(served)` (runs the session controller to `flatten_start_utc`, promotes and ticks until the session is `FLAT` or 60 iterations), used by Task 4.

- [ ] **Step 1: Write the failing tests**

```python
def scenario(served, tmp_path, **changes):
    port = RpcAcceptancePort(served.client("ai_research"), served.client("ai_supervisor"),
                             served.client("ai_supervisor", "query"), operator_client=served.client("cli", "command"),
                             now=served.now, sleep=served.advance_and_promote)
    return AcceptanceScenario(port, settings(**changes), RunJournal(tmp_path / "run"))

def test_the_spec_scenario_passes_end_to_end(served, tmp_path):
    served.sim.auto_fill(); served.sim.quote(AAPL, 229.9, 230.0); served.sim.quote(MSFT, 499.9, 500.0)
    served.sim.script_target_fills([1])                                      # S: one share fills, then nothing (Task 7)
    run = scenario(served, tmp_path).run()
    assert all(r.passed for r in run), run
    assert [r.name for r in run][-3:] == ["enter_s", "shrink_proof", "settle_s"]
    assert served.principals_for("acceptance_mark_start", "acceptance_shrink_probe") == {"cli"}      # the operator channel, not an AI key
    assert served.sim.held == {MSFT: 1.0}                                    # B left for the session flatten
    drive_session_to_flat(served)
    end = scenario(served, tmp_path).finish()
    assert all(r.passed for r in end), end
    report = served.call("ai_supervisor", "get_scoreboard", {})
    assert report["sessions"][-1]["end_state"] == "FLAT" and report["sessions"][-1]["open_positions"] == 0
    assert served.call("cli", "verify_scoreboard", {})["ok"] is True

def test_the_harness_seeds_nothing(served, tmp_path):
    served.sim.auto_fill(); scenario(served, tmp_path).run()
    ledger = served.ledger_actions()                                         # read through the stack, test-only
    assert {"register_ai_deployment", "publish_ai_risk_policy", "submit_ai_paper_decision"} <= ledger
    assert served.deployment_row_principal() == "ai_research"

def test_resume_after_a_crash_replays_and_sends_no_second_order(served, tmp_path):     # Review Focus 1
    served.sim.auto_fill()
    crashing = scenario(served, tmp_path); crashing.crash_after("enter_a")     # test hook: raise after the receipt
    with pytest.raises(SimulatedCrash):
        crashing.run()
    scenario(served, tmp_path, run_id=crashing.settings.run_id).run()
    entries = [p for p in served.sim.placed if p[0].startswith("og-aip-") and p[1] == "LMT"]
    assert len(entries) == 3                                                   # A, B and S, once each (S has its own entry, Task 7)

def test_crash_before_the_receipt_is_persisted_replays_the_stored_body(served, tmp_path):      # Review Focus 1, ruling 15
    served.sim.auto_fill()
    crashing = scenario(served, tmp_path); crashing.crash_before_receipt("enter_a")   # raise after the send, before the journal receipt
    with pytest.raises(SimulatedCrash):
        crashing.run()
    served.advance(minutes=3)                                                          # a recomputed expires_at would differ
    resumed = scenario(served, tmp_path, run_id=crashing.settings.run_id).resume()
    assert all(r.passed for r in resumed[:4])
    assert len([p for p in served.sim.placed if p[0].startswith("og-aip-") and p[1] == "LMT"]) == 3   # A, B and S once each

def test_resume_does_not_run_the_flat_account_preflight(served, tmp_path):            # ruling 15
    served.sim.auto_fill(); crashing = scenario(served, tmp_path); crashing.crash_after("enter_a")
    with pytest.raises(SimulatedCrash): crashing.run()
    assert "POSITIONS_OPEN" not in [f for r in scenario(served, tmp_path, run_id=crashing.settings.run_id).resume() for f in [r.code]]

def test_unfilled_entry_fails_the_step_without_a_retry(served, tmp_path):            # Review Focus 5
    served.sim.auto_fill(order_types=("MKT",))                                 # limits never fill
    run = scenario(served, tmp_path).run()
    assert run[-1].code == "ENTRY_NOT_PROTECTED"
    assert len([p for p in served.sim.placed if p[1] == "LMT" and "aip" in p[0]]) == 1

def test_the_report_is_signed_and_lists_every_step(served, tmp_path): ...
```

- [ ] **Step 2: Run, expect FAIL** (only Task 1–2 exist; the scenario has never met the real stack).
- [ ] **Step 3: Fix what the composed run finds.** Expected friction, each fixed in the code that owns it and named in the commit body: wire-body mismatches with Plan 3 models, the open-orders shape (`oca_group`, `oca_type`), `get_command` outcome keys. A real defect in Plans 1–5 code gets its own failing test in that plan's test file first.
- [ ] **Step 4: Run, expect PASS**, then the full suite.
- [ ] **Step 5: Commit** — `test: run the sp1 acceptance scenario through the composed stack`.

---

### Task 4: Race and restart variants through the composed system

**Files:**
- Create: `tests/sp1_acceptance/test_races.py`

**Interfaces:**
- Consumes: Tasks 1–3. Each test runs the scenario up to a named step, injects one fault with a `BrokerSim` hook or `served.restart()`, then checks the invariants of `scripts/automation_fault_drill.py:6-24` that apply: one order identity, no unsafe retry, durable breaker, reconciliation continuation, verified flat.
- Produces: `assert_no_duplicate_order_refs(served)`, `assert_breaker(served, tripped: bool)`.

- [ ] **Step 1: Write the failing tests**

```python
def test_a_stop_that_fills_instead_of_cancelling_ends_closed_with_no_reduce(served, tmp_path):
    run_until(served, tmp_path, "enter_b")
    served.sim.fill_after_cancel(stop_of(served, "A"), quantity=3)             # late fill: the stop sold all 3
    step = continue_step(served, tmp_path, "partial_close_a")
    assert (step.passed, step.code, step.evidence["liquidation_state"]) == (False, "PARTIAL_ENDED_CLOSED", "CLOSED")
    assert not [p for p in served.sim.placed if p[1] == "MKT"]                  # the close sent no reduce
    assert_no_duplicate_order_refs(served); assert_breaker(served, tripped=False)

def test_pending_cancel_that_never_lands_fails_safe_without_a_second_order(served, tmp_path):
    run_until(served, tmp_path, "enter_b")
    served.sim.pending_cancel(stop_of(served, "A"), lands=False)
    step = continue_step(served, tmp_path, "partial_close_a")
    assert step.code == "CLOSE_FAILED_SAFE"
    assert_breaker(served, tripped=True); assert_no_duplicate_order_refs(served)

def test_staging_generation_mid_flatten_waits_and_then_proves_flat(served, tmp_path):
    full_run(served, tmp_path); served.sim.reconnect()
    drive_session_to_flat(served)
    assert all(r.passed for r in scenario(served, tmp_path).finish())

def test_restart_between_reprotecting_and_done_sends_no_duplicate_legs(served, tmp_path):
    run_until(served, tmp_path, "enter_b"); served.stop_at("REPROTECTING")
    served = served.restart(); served.tick(); served.sim.promote(); served.tick()
    assert len([p for p in served.sim.placed if "-reprotect-stop" in p[0]]) == 1

def test_kill_during_reprotecting_flattens_and_writes_a_killed_row(served_with_kill, tmp_path):
    run_until(served_with_kill, tmp_path, "enter_b"); served_with_kill.stop_at("REPROTECTING")
    served_with_kill.sim.set_net_liquidation(79_000.0); served_with_kill.tick_monitor()
    drive_kill_to_flat(served_with_kill)
    assert served_with_kill.sim.held == {} and served_with_kill.equity_row()["session_end_state"] == "KILLED"
    assert served_with_kill.outbox_ids() == [f"kill_started:{served_with_kill.experiment_id}:1"]

def test_invisible_child_at_the_flatten_deadline_fails_the_run(served, tmp_path):       # Review Focus 3
    full_run(served, tmp_path); served.hide_next_child()
    drive_session_past(served, "flat_deadline_utc")
    end = scenario(served, tmp_path).finish()
    assert not all(r.passed for r in end) and end_code(end, "session_flat") == "FAILED_SAFE"
    assert_no_duplicate_order_refs(served)

def test_time_exit_and_ai_close_on_one_conid_make_one_root(served, tmp_path): ...   # Plan 1 + Plan 3 R14
def test_close_works_while_paused(served, tmp_path): ...                           # cli pauses, CLOSE A still CLOSED
def test_broker_outage_pauses_and_never_flattens(served, tmp_path): ...           # Plan 4 K23: capture fails 301 s
```

- [ ] **Step 2: Run, expect FAIL** where a fixture hook or helper is missing; every product behaviour here is already owned by Plans 1–5, so a failing assertion is a real defect: stop, add a failing test in the owning plan's test file, fix it there.
- [ ] **Step 3: Implement** the helpers (`run_until`, `continue_step`, `full_run`, `stop_at` = a `LiquidationService` hook that pauses the worker at a named state, `drive_kill_to_flat`, `drive_session_past`).
- [ ] **Step 4: Run, expect PASS**, then the full suite.
- [ ] **Step 5: Commit** — `test: race and restart variants of the sp1 acceptance scenario`.

---

### Task 5: `mmr experiment acceptance` (preflight, run, finish, status)

**Files:**
- Modify: `trader/sdk.py` (`acceptance_preflight()`), `trader/mmr_cli.py` (`experiment acceptance preflight|run|finish|status` under the Plan 4 `experiment` parser; add to the trader-service command set)
- Test: `tests/sp1_acceptance/test_cli_acceptance.py`

**Interfaces:**
- Consumes: Task 2 scenario, `evaluate_preflight` and the `get_acceptance_preflight` query; Plan 2 `ServiceIdentity.load`, `TypedRpcClient(server="trader")`.
- Produces:
  - CLI:
    - `mmr experiment acceptance preflight` (signs as `cli`; two reads 30 s apart; prints `PASS` or `STOP` and each failure; exit 0/1; `--json`).
    - `mmr experiment acceptance run [--run-id ID] [--conids A B] [--quantity-a N] [--quantity-b N] [--notional USD] [--place-orders --confirm-account DU…] [--report PATH] [--signing-key PEM]`. `--place-orders` without `--signing-key` is refused `SIGNING_KEY_REQUIRED` (ruling 20). With `--run-id` of an existing run it resumes (ruling 15: no flat-account preflight, `validate_resume`, stored bodies). Without `--place-orders` it runs the preflight and prints the planned calls, and sends nothing. Signs with `ai_research` and `ai_supervisor`, and with `--place-orders` also the operator `cli` identity (`cli.key`), each loaded by `ServiceIdentity.load(p)` from `MMR_RPC_KEYS_DIR` or `~/.config/mmr/keys/rpc`; the `cli` identity is used only for `acceptance_mark_start` and `acceptance_shrink_probe`.
    - `mmr experiment acceptance finish --run-id ID [--report PATH] --signing-key PEM` (required); `mmr experiment acceptance status --run-id ID` (reads the local journal only); `mmr experiment acceptance verify-report PATH --public-key PEM` (checks the Ed25519 signature offline, prints `signature OK` and the fields `signing_key`, `evidence_source`, `deployment_record`, `live_restart_recovery`, `oca_shrink`; exit 1 on a bad signature or an ephemeral key).
    - Run directory `~/.local/share/mmr/acceptance/<run_id>/` (journal, `report.json`).

- [ ] **Step 1: Write the failing tests**

```python
# test_cli_acceptance.py — real served stack, keys written with write_keyset into MMR_RPC_KEYS_DIR
def test_dry_run_sends_no_command(served_with_keys, cli):
    out = cli("experiment acceptance run")
    assert "dry run" in out and served_with_keys.command_count() == 0

def test_run_refuses_wrong_account_and_container(served_with_keys, cli, monkeypatch):     # Review Focus 4
    assert "ACCOUNT_MISMATCH" in cli("experiment acceptance run --place-orders --confirm-account DU999999")
    monkeypatch.setattr("os.path.exists", lambda p: p == "/.dockerenv" or os.path.lexists(p))
    assert "host only" in cli("experiment acceptance run --place-orders --confirm-account DU111111")
    assert served_with_keys.command_count() == 0

def test_missing_supervisor_key_refuses_before_any_call(served_with_keys, cli, rpc_keys_dir):
    (rpc_keys_dir / "ai_supervisor.key").unlink()
    assert "ai_supervisor.key" in cli("experiment acceptance run --place-orders --confirm-account DU111111")

def test_missing_cli_key_refuses_a_real_run_but_not_the_dry_run(served_with_keys, cli, rpc_keys_dir):   # ruling 2: cli is loaded only with --place-orders
    (rpc_keys_dir / "cli.key").unlink()
    assert "cli.key" in cli("experiment acceptance run --place-orders --confirm-account DU111111") and served_with_keys.command_count() == 0
    assert "dry run" in cli("experiment acceptance run")

def test_run_and_finish_end_to_end_over_the_cli(served_with_keys, cli): ...     # auto_fill sim; finish after drive_session_to_flat
def test_preflight_prints_stop_and_exits_one_when_not_clean(served_with_keys, cli): ...
def test_place_orders_without_a_signing_key_is_refused_before_any_call(served_with_keys, cli):   # ruling 20
    assert "SIGNING_KEY_REQUIRED" in cli("experiment acceptance run --place-orders --confirm-account DU111111") and served_with_keys.command_count() == 0
def test_a_real_report_signed_with_an_ephemeral_key_does_not_verify_as_the_operator(...): ...
def test_status_reads_the_local_journal_only(cli, tmp_path): ...
```

- [ ] **Step 2: Run, expect FAIL.**
- [ ] **Step 3: Implement.** The CLI handler order for `run`: host check → load the identities: `ai_research` and `ai_supervisor`, plus `cli` with `--place-orders` (fail with the file name) → preflight (`evaluate_preflight` on two reads 30 s apart) → experiment `ARMED` → account equals `--confirm-account` → scenario. Exit code 0 only when every step passed; the signed report is written in every case.
- [ ] **Step 4: Run, expect PASS**, then `tests/test_rpc_acl.py` and the full suite.
- [ ] **Step 5: Commit** — `feat: add mmr experiment acceptance`.

---

### Task 6: `mmr flatten` for the abort path

**Files:**
- Modify: `trader/sdk.py` (`flatten(reason: str, command_id: Optional[str] = None) -> dict` over `_typed_command("liquidate_account", ...)`; `wait_flat(timeout) -> dict`), `trader/mmr_cli.py` (`flatten --reason TEXT [--wait] [--yes]` next to `cancel-all`)
- Test: `tests/test_mmr_cli_flatten.py`

**Interfaces:**
- Consumes: typed `liquidate_account` (`LiquidateAccountRequest`, `production_api.py:568-579`; registered at `:1880-1888`, preflight nonce only on live), `get_command`, `get_positions`, `get_open_orders`.
- Produces: CLI prints the root id, then with `--wait` polls until the command resolves and the broker shows no position and no working order (300 s), printing `FLAT` or the last state. Without `--yes` it asks `Type FLATTEN to close every position on <account>`. A live account is refused by the CLI (`LIVE_REFUSED`); live flatten stays on the dashboard path with its preflight.

- [ ] **Step 1: Write the failing tests**

```python
def test_flatten_needs_confirmation(cli, sdk): assert "Type FLATTEN" in cli("flatten --reason abort", stdin="no\n") and sdk.calls == []
def test_flatten_sends_liquidate_account_with_a_fresh_command_id(cli, sdk):
    cli("flatten --reason abort --yes"); (method, body), = sdk.calls
    assert method == "liquidate_account" and body["reason"] == "abort" and ":" not in body["command_id"]
def test_flatten_wait_reports_flat_only_on_broker_evidence(cli, sdk): ...   # resolved command but a working order -> not FLAT
def test_flatten_refuses_a_live_account(cli, sdk): ...
def test_flatten_over_the_served_stack_reaches_flat(served_with_keys, cli): ...   # Task 1 sim, real liquidation
```

- [ ] **Step 2: Run, expect FAIL.**
- [ ] **Step 3: Implement** as specified; `command_id = "flatten-" + uuid4().hex[:16]`.
- [ ] **Step 4: Run, expect PASS**, then the full suite.
- [ ] **Step 5: Commit** — `feat: add mmr flatten over the typed liquidate_account command`.

---

### Task 7: The live OCA shrink proof (spec 5.1, ruling 13)

**Files:**
- Create: `trader/acceptance/shrink_proof.py`, `trader/trading/acceptance_probe.py`, `tests/sp1_acceptance/test_shrink_proof.py`
- Modify: `trader/messaging/production_api.py` (commands `acceptance_mark_start`, `acceptance_shrink_probe`), `trader/messaging/principals.py` (`("command","acceptance_shrink_probe")` and `("command","acceptance_mark_start")`: each `frozenset({"cli"})`; the harness calls it through the operator `cli` identity, which `mmr experiment acceptance run --place-orders` loads as a third key), `trader/acceptance/scenario.py` (steps `enter_s`, `shrink_proof`, `settle_s`), `trader/acceptance/ports.py` (the `operator` channel), `trader/data/schema_migrations.py` (docstring) and `trader/acceptance/migrations.py` (new: `apply_migration_80_acceptance_marks(migrator)` and `apply_migration_81_broker_order_events(migrator)`, each calling `SchemaMigrator.apply(version, name, statements)` with `CREATE TABLE IF NOT EXISTS` and returning its bool; the trader's journal start-up calls both), `trader/acceptance/report.py`

**Interfaces:**
- Consumes: Task 2 `get_broker_order_evidence` and `get_experiment_trips`; Plan 3 `submit_ai_paper_decision` (`ENTER` S with id `<run_id>-e-s`, 3 shares, the same notional refusal); Plan 1 exit placement (the bot's own stop and target in one `ocaType=2` group).
- Produces:
  - Command `acceptance_mark_start {command_id, experiment_id, run_id, conid, decision_id}` (ACL `{"cli"}`; sent once by `acceptance run --place-orders` on a first run, **after** `enter_s` has proven both S legs working (`await_s_protected`) and immediately before the probe; never before the entry, and never when `enter_s` failed; a resume never sends it). It is the only writer of the durable acceptance mark: a row in `acceptance_marks(experiment_id PRIMARY KEY, run_id, conid, decision_id, created_at, expires_at, consumed_at)` in the trader's journal database (`trader.journal_db`, trader-owned; migration **80**), with `expires_at = created_at + 24 h`. It refuses unless paper, the experiment is `ARMED` and equals `experiment_id`, `ai_paper.acceptance_probe` is true, `decision_id == <run_id>-e-s`, and no mark exists for that experiment. Refusals: `PROBE_NOT_ENABLED`, `PROBE_NOT_PAPER`, `PROBE_NOT_ARMED`, `PROBE_MARK_EXISTS`.
  - Command `acceptance_shrink_probe {command_id, experiment_id, run_id, conid, decision_id, display_size: 1}`. The trader refuses unless: paper account, `ai_paper.acceptance_probe: true`, the experiment is `ARMED`, a durable mark exists for it and matches the body exactly (experiment, run, conid, decision id) and has not expired or been consumed, the open position is exactly 3 shares of the conid and its entry came from the mark's decision id (ledger lookup), and there is exactly one working `take_profit` and one `stop` in one group with `oca_type == 2`. The flag and the `cli` ACL alone are not the scope; the mark is. It then **modifies only the target** and consumes the mark in the same transaction that records the intent (one probe per mark; a crash after consume is `UNPROVEN`, not retried):
    - It copies the broker-confirmed working target `Order` from the promoted generation. It changes only `lmtPrice` (current bid) and `displaySize` (1). It keeps `orderId`, `permId`, `ocaGroup`, `ocaType`, `totalQuantity`, `action`, `orderType`, `tif`, `parentId`. `ib_async` `placeOrder` with an existing `orderId` is a whole-order replace, and a freshly built delta `Order` has `ocaGroup=''` and `ocaType=0`, which would clear the link. A two-key delta is never sent.
    - After the modify it reads the broker evidence again (a new promoted generation read). If the target or the stop has `oca_type != 2`, the group is missing or differs, or the stop changed, it returns `PROBE_OCA_LOST`, the harness does **not** start the 60 s wait, and the runbook abort A4 flattens.
    - It never changes quantity, never adds an order and never touches the stop.
    Refusal codes: `PROBE_NOT_ENABLED`, `PROBE_NOT_PAPER`, `PROBE_NOT_ARMED`, `PROBE_MARK_MISSING` (no mark for the experiment), `PROBE_MARK_MISMATCH` (any body field differs from the mark, or the position's entry is not the mark's decision), `PROBE_MARK_STALE` (expired, already consumed, or the experiment is no longer the marked one), `PROBE_POSITION_MISMATCH`, `PROBE_OCA_NOT_FOUND`, `PROBE_OCA_TYPE_NOT_2`, `PROBE_OCA_LOST` (after the modify).
  - `scenario.build_entry_s(settings, ask) -> dict`: S's own `ENTER` body. `decision_id = <run_id>-e-s`, `quantity = 3`, `stop_price = round(ask × 0.98, 2)`, `target_price = round(ask × 1.02, 2)` (the profit side of the ask, so the target does not fill by itself), the same notional refusal and expiry as A and B. Task 2's builder (no target) is never used for S.
  - Step `enter_s` then calls `await_s_protected`: it polls `get_broker_order_evidence(conid)` until it sees, on one promoted generation, exactly one working `stop` and one working `take_profit` for S, each acknowledged by the broker (`perm_id` set, status `Submitted` or `PreSubmitted`), `oca_type == 2`, the same non-empty `oca_group`, quantity 3 each, and `get_positions` shows 3. Timeout 120 s or any other state: step `FAIL` `S_NOT_PROTECTED_WITH_TARGET`, `acceptance_mark_start` is not sent and the probe is never called.
  - Step `settle_s` (always runs after `shrink_proof`, for `PROVEN`, `UNPROVEN` and `FAILED` alike, except that on `OCA_SIBLING_NOT_SHRUNK` abort A4 flattens instead): it first reads the broker evidence. **Already flat:** if a promoted generation shows S flat (`get_positions` 0) and no working order for S's conid (for example a whole target fill, after which OCA cancelled the stop), it records the settlement (`settled_by: "already_flat"`, with the generation id) and sends **no** CLOSE and opens no close root; Plan 3 refuses a CLOSE with no held position (`NOT_A_REDUCTION`). **Still held or exits still working:** it sends the single stored `CLOSE` decision `<run_id>-c-s` without quantity (replayed from the journal, never rebuilt), which goes through the safe close (Plan 1: cancel or join S's live stop and target, close the residual, prove flat), and waits for `get_experiment_trips` and the evidence read to show S flat, no working order for S's conid, and the close root complete, within 120 s. Either way the settlement does not change the shrink result: an `UNPROVEN` shrink stays `UNPROVEN` and the acceptance FAIL, only a `PROVEN` shrink passes. It never waits for the target to finish. The run phase is complete only when `settle_s` passes; otherwise `S_NOT_FLAT` and abort A2.
  - `shrink_proof.run_shrink_proof(port, settings, journal) -> StepResult`: polls `port.evidence(conid)` every 0.5 s for at most 60 s and journals every distinct reading with its `generation_id`. Decision table, evaluated on each reading including every `status_events` entry:
    - `PROVEN`: a target event E (`0 < filled_quantity < 3`) and a stop event F with `remaining_quantity == 3 - filled_quantity(E)`, both read from `status_events` of the same promoted generation with `F.cursor > E.cursor`, `oca_type == 2` on both legs, same `oca_group`, and `get_positions` equal to the residual. Each event is judged with its own stored quantities. A pair needing a current-row quantity combined with an older status string is not a pair.
    - `FAILED` (`OCA_SIBLING_NOT_SHRUNK`): a partial fill and the stop's remaining quantity still 3 (or any value other than the residual). The harness stops polling and returns; the runbook abort A4 flattens.
    - `UNPROVEN` (`OCA_SHRINK_UNPROVEN`): no partial event was observed (whole fill in one event, or no fill within 60 s), or the events needed to see the transition were not recorded (cursor gap, generation change, missing stop event). The harness then runs `settle_s` only (no retry, resize or instrument change): `settled_by: already_flat` when a promoted generation proves S flat with no working S orders (no CLOSE, no close root), otherwise `settled_by: close` (one stored CLOSE, completed root). UNPROVEN stays a failed acceptance result either way.
  - Report: `oca_shrink` set from the result, `evidence_source` `ib_paper` only when the port is the RPC port to a non-simulated broker (`generation.source == "ib"`), otherwise `synthetic`; a synthetic `PROVEN` is written as `evidence_source: "synthetic"` and `passed` stays false for a real-session report.

- [ ] **Step 1: Write the failing tests**

```python
def test_a_partial_fill_with_a_shrunk_sibling_is_proven(fake_port):
    fake_port.evidence_script([target(filled=1, remaining=2), stop(remaining=2)])
    assert run_shrink_proof(fake_port, settings, journal).evidence["oca_shrink"] == "PROVEN"

def test_a_partial_fill_with_an_unchanged_sibling_fails_and_stops(fake_port):
    fake_port.evidence_script([target(filled=1, remaining=2), stop(remaining=3)])
    r = run_shrink_proof(fake_port, settings, journal)
    assert (r.passed, r.code) == (False, "OCA_SIBLING_NOT_SHRUNK") and fake_port.methods().count("submit_ai_paper_decision") == 0

def test_a_whole_fill_in_one_event_is_unproven_never_a_pass(fake_port):
    fake_port.evidence_script([target(filled=3, remaining=0)])
    r = run_shrink_proof(fake_port, settings, journal)
    assert (r.passed, r.code) == (False, "OCA_SHRINK_UNPROVEN")

def test_no_fill_in_60_seconds_is_unproven_and_sends_no_retry(fake_port): ...
def test_oca_type_other_than_2_fails(fake_port): ...                      # OCA_TYPE_NOT_2
def test_a_staging_generation_reading_is_ignored_not_counted(fake_port): ...
def test_a_synthetic_proven_never_marks_the_real_session_accepted(fake_port):
    assert build_report(oca_shrink="PROVEN", evidence_source="synthetic").passed is False

def test_the_probe_sends_the_whole_target_order_with_only_price_and_display_size_changed(served):   # composed stack, Task 1 sim records the outgoing Order
    enter_s(served); mark_start(served); target_before = served.sim.working_order("take_profit")
    served.call("cli", "acceptance_shrink_probe", probe_body(served))
    sent = served.sim.modified[-1]                                                  # the actual outgoing Order, not a delta
    assert (sent.orderId, sent.ocaGroup, sent.ocaType, sent.totalQuantity, sent.action, sent.orderType) == \
           (target_before.orderId, target_before.ocaGroup, 2, 3, target_before.action, target_before.orderType)
    assert (sent.displaySize, sent.lmtPrice) == (1, served.sim.bid(AAPL)) and served.sim.placed_count_unchanged()
    assert served.sim.working_order("stop") == served.sim.stop_before                # stop untouched

def test_a_modify_that_clears_the_oca_link_is_refused_before_any_wait(served):
    enter_s(served); mark_start(served); served.sim.modify_clears_oca()              # broker returns ocaType 0 after the modify
    r = served.call("cli", "acceptance_shrink_probe", probe_body(served))
    assert r["code"] == "PROBE_OCA_LOST" and served.sim.sleeps_after_probe == 0

@pytest.mark.parametrize("fault,code", [("not_enabled","PROBE_NOT_ENABLED"), ("live","PROBE_NOT_PAPER"), ("not_armed","PROBE_NOT_ARMED"),
                                        ("position_4","PROBE_POSITION_MISMATCH"), ("no_group","PROBE_OCA_NOT_FOUND"), ("type_1","PROBE_OCA_TYPE_NOT_2")])
def test_probe_refusals(served, fault, code): ...

def test_a_missing_mark_refuses_the_probe(served):                                   # flag on, cli key, ARMED 3-share OCA position, but no acceptance start
    enter_s(served)
    assert served.call("cli", "acceptance_shrink_probe", probe_body(served))["code"] == "PROBE_MARK_MISSING" and served.sim.modified == []

@pytest.mark.parametrize("field", ["experiment_id", "run_id", "conid", "decision_id"])
def test_a_wrong_mark_refuses_the_probe(served, field):
    enter_s(served); mark_start(served)
    assert served.call("cli", "acceptance_shrink_probe", probe_body(served, **{field: wrong(field)}))["code"] == "PROBE_MARK_MISMATCH"

def test_a_position_not_entered_by_the_marked_decision_refuses_the_probe(served):
    mark_start(served); enter_other_3_share_position(served)
    assert served.call("cli", "acceptance_shrink_probe", probe_body(served))["code"] == "PROBE_MARK_MISMATCH"

def test_an_expired_consumed_or_replaced_mark_is_stale(served):
    enter_s(served); mark_start(served); served.clock.advance(hours=25)
    assert served.call("cli", "acceptance_shrink_probe", probe_body(served))["code"] == "PROBE_MARK_STALE"
    # a second probe on a consumed mark, and a probe after the experiment was stopped and a new one armed, also return PROBE_MARK_STALE

def test_only_acceptance_mark_start_writes_the_mark(served):
    served.call("cli", "start_experiment", {...}); assert served.store.marks() == []   # no other command creates it
    assert served.call("ai_supervisor", "acceptance_mark_start", mark_body(served))["code"] == "PERMISSION_DENIED"
def test_s_entry_body_has_stop_and_target_on_the_right_sides(settings):
    body = build_entry_s(settings, ask=200.0)
    assert (body["decision_id"], body["action"], body["quantity"], body["stop_price"], body["target_price"]) == (f"{settings.run_id}-e-s", "ENTER", 3, 196.0, 204.0)

def test_the_probe_is_not_called_until_both_oca_legs_are_working(fake_port, settings, journal):
    fake_port.evidence_script_for_enter_s(stop=True, target=False)               # stop-only bracket
    r = AcceptanceScenario(fake_port, settings, journal).run_from("enter_s")
    assert r[-1].code == "S_NOT_PROTECTED_WITH_TARGET" and "acceptance_mark_start" not in fake_port.methods() and "acceptance_shrink_probe" not in fake_port.methods()

def test_s_gets_two_linked_working_legs_through_the_composed_stack(served):
    enter_s(served); rows = served.call("ai_supervisor", "get_broker_order_evidence", {"conid": AAPL})["orders"]
    legs = {r["leg"]: r for r in rows if r["leg"] in ("stop", "take_profit")}
    assert set(legs) == {"stop", "take_profit"} and legs["stop"]["oca_group"] == legs["take_profit"]["oca_group"] != ""
    assert {l["oca_type"] for l in legs.values()} == {2} and all(l["perm_id"] and l["total_quantity"] == 3 for l in legs.values())

def test_unproven_whole_fill_flat_settles_without_a_close_and_the_run_fails(fake_port, settings, journal):     # Task 2 runner exception
    fake_port.evidence_script_whole_fill_in_one_event()                         # target filled 3 in one event, stop cancelled by OCA, S flat
    results = AcceptanceScenario(fake_port, settings, journal).run()
    assert [r.name for r in results][-2:] == ["shrink_proof", "settle_s"] and results[-2].code == "OCA_SHRINK_UNPROVEN"
    assert results[-1].passed and results[-1].evidence["settled_by"] == "already_flat"
    assert f"{settings.run_id}-c-s" not in fake_port.decision_ids()                 # no CLOSE sent: Plan 3 would refuse NOT_A_REDUCTION
    assert AcceptanceScenario.outcome(results).passed is False and AcceptanceScenario.outcome(results).oca_shrink == "UNPROVEN"
    assert fake_port.calls.count(("operator", "acceptance_shrink_probe")) == 1 and fake_port.decision_ids().count(f"{settings.run_id}-e-s") == 1   # no retry, no new entry

def test_unproven_no_fill_still_held_sends_the_single_close_and_the_run_fails(fake_port, settings, journal):
    fake_port.evidence_script_no_fill_in_60s(held=3)
    results = AcceptanceScenario(fake_port, settings, journal).run()
    assert results[-2].code == "OCA_SHRINK_UNPROVEN" and results[-1].name == "settle_s" and results[-1].passed
    assert fake_port.decision_ids().count(f"{settings.run_id}-c-s") == 1 and fake_port.calls[-1] == ("supervisor", "submit_ai_paper_decision")
    assert AcceptanceScenario.outcome(results).passed is False and AcceptanceScenario.outcome(results).oca_shrink == "UNPROVEN"
    assert fake_port.calls.count(("operator", "acceptance_shrink_probe")) == 1 and fake_port.decision_ids().count(f"{settings.run_id}-e-s") == 1

def test_sibling_not_shrunk_stops_at_once_without_settle(fake_port, settings, journal):
    fake_port.evidence_script([target(filled=1, remaining=2), stop(remaining=3)])
    assert [r.name for r in AcceptanceScenario(fake_port, settings, journal).run()][-1] == "shrink_proof"      # abort A4 flattens

def test_migrations_80_and_81_apply_once_on_a_real_connection(tmp_path):
    from trader.data.duckdb_store import DuckDBConnection
    from trader.data.schema_migrations import SchemaMigrator
    from trader.acceptance.migrations import apply_migration_80_acceptance_marks, apply_migration_81_broker_order_events
    migrator = SchemaMigrator(DuckDBConnection(str(tmp_path / "journal.duckdb")))
    migrator.apply(70, "experiments_stub", ["CREATE TABLE IF NOT EXISTS experiments_stub (id INTEGER)"])        # a journal that already holds earlier plans
    assert apply_migration_80_acceptance_marks(migrator) is True and apply_migration_81_broker_order_events(migrator) is True
    assert {80, 81} <= migrator.applied_versions() and {70, 80, 81} <= migrator.applied_versions()
    assert apply_migration_80_acceptance_marks(migrator) is False and apply_migration_81_broker_order_events(migrator) is False   # idempotent: second run is a no-op
    for table in ("acceptance_marks", "broker_order_events"):
        assert migrator.db.execute(f"SELECT COUNT(*) FROM {table}", fetch="one")[0] == 0

def test_a_partial_fill_then_no_more_fills_closes_the_residual_and_the_run_continues(served, tmp_path):
    enter_s(served); served.sim.script_target_fills([1])                         # one share fills, nothing after
    results = scenario(served, tmp_path).run_from("shrink_proof")
    assert [r.name for r in results] == ["shrink_proof", "settle_s"] and results[0].evidence["oca_shrink"] == "PROVEN"
    assert results[1].passed and results[1].evidence["settled_by"] == "close" and served.positions(AAPL) == 0 and served.working_orders(AAPL) == []
    assert AcceptanceScenario.outcome(results).passed is True                    # settle_s is the last step; the run outcome is the pass
    # the full run then continues with the session flatten of B and the finish checks (Task 3 end-to-end test)

def test_settle_s_runs_after_unproven_and_fails_the_run_phase_when_s_is_not_flat(served, tmp_path):
    enter_s(served); served.sim.never_close(AAPL)
    assert scenario(served, tmp_path).run_from("settle_s")[0].code == "S_NOT_FLAT"

def test_the_probe_is_denied_to_ai_principals(served): ...                           # ai_supervisor, ai_research -> PERMISSION_DENIED (both commands)
def test_end_to_end_synthetic_partial_run_through_the_composed_stack(served, tmp_path): ...  # proves the logic only; evidence_source == "synthetic"
```

- [ ] **Step 2: Run, expect FAIL.**
- [ ] **Step 3: Implement.** First step of the implementation: confirm with a read of `ib_async` (`IB.placeOrder` with an existing `orderId` sends the whole `Order`) and Plan 1's order placement that a modify built by copying the working target keeps its `ocaGroup` and `ocaType`; if the broker interface cannot express it, stop and report to the owner instead of inventing another way to force a partial fill (the gate then stays `UNPROVEN`). Add the `broker_order_events` append to the broker-order ingest (same transaction as the projection update) and test: one event per callback, gapless `cursor`, rows of a staging generation never returned.
- [ ] **Step 4: Run, expect PASS**, then the full suite.
- [ ] **Step 5: Commit** — `feat: add the live oca shrink proof step to the sp1 acceptance harness`.

---

### Task 8: The paper-session runbook

**Files:**
- Create: `docs/PAPER_ACCEPTANCE_SP1.md` (the text below), `tests/test_acceptance_runbook.py`
- Modify: `docs/OPERATIONAL_STATE.md` (link under "Next operator session"), `AGENTS.md` (one paragraph "**SP1 acceptance**" under "Key Patterns": `trader/acceptance/`, the two drivers, the gate, `mmr experiment acceptance …` and `mmr flatten` under "CLI Commands" and "Requires trader typed RPC")

**Interfaces:**
- Consumes: Tasks 5–6 commands; Plans 2, 4, 5 commands (`./docker.sh -k`, `-b`, `-u`, `-B`; `mmr experiment start|status|pause|stop`; `mmr scoreboard verify`; `mmr --json scoreboard`).
- Produces: `tests/test_acceptance_runbook.py::test_every_mmr_command_in_the_runbook_parses` (extracts each `mmr …` command in a table cell or fenced line, including the one after `docker compose run --rm scheduler`, and parses it with `trader.mmr_cli.build_parser()`; `<…>` placeholders replaced by sample values) and `test_every_docker_sh_flag_in_the_runbook_exists` (each `./docker.sh -X` flag appears in `docker.sh`'s option parser).

- [ ] **Step 1: Write the failing tests** (the two above).
- [ ] **Step 2: Run, expect FAIL** (no runbook yet).
- [ ] **Step 3: Write `docs/PAPER_ACCEPTANCE_SP1.md`** with exactly this content (adjust only command spellings that the parse test proves wrong):

````markdown
# SP1 paper acceptance — owner runbook

One real IB paper session proves SP1 before SP2 trades (spec section 6). Only the
owner runs it. Every order in it comes from `mmr experiment acceptance run
--place-orders`; nothing else places orders. Times are US Eastern on a normal
(non-half) XNYS day. Paper account only.

## Before the day (any time)

| Step | Command | Pass | Fail → |
|---|---|---|---|
| P0.1 build | `./docker.sh -b` | exit 0 | stop, fix the build |
| P0.2 keys | `./docker.sh -k` | `~/.config/mmr/keys/rpc/` has `trader`, `strategy`, `cli`, `dashboard`, `ai_supervisor`, `ai_research` `.key` (0600) and `.pub` | stop |
| P0.3 cutover gate (Plan 2) | `docker compose --profile test run fullstack-tests` | all pass | stop; do not `-u` |
| P0.4 config | in `~/.config/mmr/trader.yaml`: `trading_mode: paper`, `ai_paper.enabled: true`, `ai_paper.telegram.enabled: false` (unless you supplied a bot and chat id; the report then says `telegram_live_delivery: UNTESTED`), `ai_paper.acceptance_probe: true` (needed by P4S; set it back to `false` after P6.4; Plan 3 Task 3 parses it, default `false`, so the key is legal), `experiment_kill_drawdown_pct` as you choose | `mmr status` after P1.1 shows paper and trader_service starts with the key present (an unknown-key config error here means Plan 3 Task 3 is missing); `mmr status --json` shows `ai_paper.acceptance_probe: true` | fix the file |
| P0.6 operator key | `mkdir -p ~/.config/mmr/keys/acceptance && openssl genpkey -algorithm ed25519 -out ~/.config/mmr/keys/acceptance/operator.key && openssl pkey -in ~/.config/mmr/keys/acceptance/operator.key -pubout -out ~/.config/mmr/keys/acceptance/operator.pub && chmod 600 ~/.config/mmr/keys/acceptance/operator.key` (once; keep both files, back them up) | both files exist | stop: the real report cannot be signed |
| P0.5 history | after P1.2, inside the scheduler container (the trader reads the `mmr_db_data` volume, not a host file): `docker compose run --rm scheduler mmr data download SPY --bar-size "1 day" --days 30` | rows written | scoreboard SPY shows unknown; not a blocker |

## Session day

### P1 — start and smoke (09:00–09:30)

| Step | Command | Pass | Fail → |
|---|---|---|---|
| P1.1 backup | `./docker.sh -B before_acceptance` | backup file named | stop |
| P1.2 start | `./docker.sh -u` | containers up | abort A1 |
| P1.3 status | `mmr status` | trader reachable, `ib_upstream_connected: true`, paper | VNC to the gateway (port 5901), then retry; else stop |
| P1.4 live feed | `mmr --json snapshot AAPL --source ib` and `mmr --json snapshot MSFT --source ib` | bid and ask present, not delayed | stop: every `ENTER` would be refused `FEED_NOT_LIVE` |
| P1.5 read smoke | `scripts/paper_e2e.sh tests/paper_e2e/test_stack_gate.py tests/paper_e2e/test_typed_rpc_queries.py` (with `MMR_PAPER_E2E_LIVE_ORDERS` **unset**) | all pass or skip with a reason | stop |

### P2 — the clean-account gate (#34: no IB reset by default)

| Step | Command | Pass | Fail → |
|---|---|---|---|
| P2.1 gate | `mmr experiment acceptance preflight` | prints `PASS`, exit 0 | prints `STOP` and the reasons: **stop here**. Cleanup is your decision: `mmr flatten --reason "pre-acceptance cleanup" --wait`, cancel orders in the dashboard (`/cc` → cancel all), `mmr reconcile`; or an IB paper-account reset in Client Portal (takes effect the next day). Then run P2.1 again. |

The gate passes only if: paper account (`DU…`); no position; no working order;
no unresolved command; no open liquidation or exit owner; breaker clear; net
liquidation finite, above zero, at most 5 minutes old, stable within 0.1 % over
30 seconds; daily P&L known; base currency USD or a USD rate.

### P3 — arm (09:35–09:40)

| Step | Command | Pass | Fail → |
|---|---|---|---|
| P3.1 arm | `mmr experiment start --reason "sp1 acceptance"` | state `ARMED`, start net liquidation shown | the refusal code says why; fix, re-run P2.1, retry |
| P3.2 confirm | `mmr --json experiment status` | `ARMED`, `kill_line.active` as configured, `pending_restart: false`, `mode_conflict: null` | `mmr experiment stop --reason "arm check failed"`; stop |

### P4 — the scenario (09:40–14:30)

| Step | Command | Pass | Fail → |
|---|---|---|---|
| P4.1 dry run | `mmr experiment acceptance run` | prints the planned calls, "dry run", sends nothing | stop |
| P4.2 run | `mmr experiment acceptance run --place-orders --confirm-account <DU account> --signing-key ~/.config/mmr/keys/acceptance/operator.key --report ~/.local/share/mmr/acceptance/run-report.json` | every step `PASS` including `shrink_proof` = `PROVEN`; ends with "B open, protected; waiting for the session flatten"; note the printed `run_id`. `OCA_SHRINK_UNPROVEN`: the session is **not** acceptance; flatten (A2), stop, try another day; do not retry today. `OCA_SIBLING_NOT_SHRUNK`: abort A4 at once | abort A2 |
| P4.3 orders | `mmr --json orders` | only B's protective stop is working | abort A2 |

What P4.2 does, in order: register the catalogue deployment (`ai_research`);
publish the paper limits (`ai_supervisor`); `ENTER` A (3 shares) and B (1
share) with explicit sizes; `PARTIAL_CLOSE` 1 share of A (stop and target
re-placed for 2 shares in one OCA group); `CLOSE` the rest of A; then P4S below. B stays open.
Before each entry the harness resolves the conid, reads the live ask and refuses
when `quantity x ask` is above the 2,000 USD notional. It never raises the notional.

Evidence to look at during P4: after the partial close, the report's `order_evidence`
(from the typed broker-order evidence read; `mmr orders` does not show OCA fields)
lists A's stop and target with the same OCA group, `oca_type` 2 and remaining 2 each. If an entry filled only partly and the AI cutoff cancel
(15:30) cancelled the rest, record whether IB shrank the bracket children.

#### P4S — the live OCA shrink proof (mandatory; part of P4.2, spec 5.1)

The harness does this inside `run` after `CLOSE` A. It is not optional, and a session
without `PROVEN` is not SP1 acceptance.

| Step | What happens | Pass | Fail → |
|---|---|---|---|
| P4S.1 enter S | `ENTER` AAPL 3 shares (`<run_id>-e-s`) with stop at ask x 0.98 and target at ask x 1.02; bot places both in one `ocaType=2` group | evidence read: stop and target both acknowledged and working, same non-empty group, `oca_type` 2, quantity 3, position 3; only then does the harness send the mark and the probe | `S_NOT_PROTECTED_WITH_TARGET`: abort A2 |
| P4S.2 probe | after P4S.1 passed, the harness sends `acceptance_mark_start`, then the trader modifies a copy of S's target: limit at the bid, display size 1, OCA fields kept (`acceptance_shrink_probe` with the run's experiment id, run id, conid and decision id); the trader re-reads the broker | command accepted, `oca_type` still 2 | `PROBE_OCA_LOST`: abort A4 at once; other `PROBE_*` refusal: abort A2; gate `UNPROVEN` |
| P4S.3 observe | harness reads the evidence every 0.5 s for 60 s, journalling each reading | a target event with 0 < filled < 3 and the stop's remaining equal to 3 - filled, position equal to it, same generation: `oca_shrink: PROVEN` | no partial event: `OCA_SHRINK_UNPROVEN`, flatten (A2), no retry today. Partial but stop unchanged: `OCA_SIBLING_NOT_SHRUNK`, abort A4 |
| P4S.4 settle | whatever the proof result, the harness runs `settle_s`; it never waits for the target to finish. If a promoted generation already shows S flat with no working S orders (e.g. a whole target fill), it records `settled_by: already_flat` and sends **no** CLOSE (a CLOSE with no position is refused `NOT_A_REDUCTION`). Otherwise it sends the stored `CLOSE <run_id>-c-s` once (safe close cancels or joins the live stop and target) and records `settled_by: close` | position 0 and no working order for S on a promoted generation; for `close` also a complete close root; S one closed trip (fills plus any close). UNPROVEN still fails the gate | `S_NOT_FLAT`: abort A2 |

Manual cross-check (keep it with the evidence): in Client Portal, Reports, Trade
Confirmation, S's target shows several executions of 1 share, and TWS/Gateway order
history shows the stop quantity falling with them. The harness never engineers a
partial fill by size, symbol or price beyond the one display-size change above.

### P5 — the session flatten (15:30–15:55, hands off)

| Time | What happens | Check |
|---|---|---|
| 15:30 | entry cutoff; AI entries cancelled | `mmr --json orders`: no AI entry working |
| 15:45 | session flatten starts | — |
| by 15:55 | flat proven on a broker generation | `mmr --json portfolio`: no position; `mmr --json orders`: nothing working |

Do not trade in this window. If 15:55 passes with a position or order: abort A3.

### P6 — finish and verify (after 15:55)

| Step | Command | Pass | Fail → |
|---|---|---|---|
| P6.1 finish | `mmr experiment acceptance finish --run-id <run_id> --signing-key ~/.config/mmr/keys/acceptance/operator.key --report ~/.local/share/mmr/acceptance/finish-report.json` | every end check `PASS` (`equity_daily` row `FLAT`, `open_positions` 0, no position, no working order, trips through `get_experiment_trips`: A one closed trip of 3 shares, S closed, 3 shares in total (the partial fills plus the settle close), B one closed trip of 1 share, `oca_shrink` `PROVEN`, no incidents) | record; the run failed |
| P6.2 verify | `mmr scoreboard verify` | exit 0, no mismatch | record; the run failed |
| P6.3 scoreboard | `mmr --json scoreboard > ~/.local/share/mmr/acceptance/scoreboard.json` | label `PAPER`, today `FLAT` | record |
| P6.4a verify the report | `mmr experiment acceptance verify-report ~/.local/share/mmr/acceptance/finish-report.json --public-key ~/.config/mmr/keys/acceptance/operator.pub` | `signature OK`, `signing_key: operator`, `evidence_source: ib_paper`, `deployment_record: harness_fixture`, `live_restart_recovery: NOT_PROVEN` | the run failed |
| P6.4 stop | `mmr experiment stop --reason "sp1 acceptance done"` | `STOPPED` | `NOT_FLAT`: abort A3 |
| P6.5 backup | `./docker.sh -B after_acceptance` | backup file named | — |

## Evidence to keep

Copy into `~/.local/share/mmr/acceptance/<run_id>/` (the harness already wrote
`journal.jsonl` and the reports there):

- `run-report.json`, `finish-report.json` (signed);
- `scoreboard.json` and the output of `mmr scoreboard verify`;
- `mmr --json experiment status` before arming and after stopping;
- the `order_evidence` rows in the signed reports (OCA group, type and remaining quantity, with the `status_events` of S's target and stop);
- the trader log of the day: `~/.local/share/mmr/logs/trader_service_*.log`;
- the IB paper trade confirmations for the day (Client Portal → Reports →
  Trade Confirmation), downloaded by you;
- the two backup names (`before_acceptance`, `after_acceptance`).

The session passes only if P4.2 (with `oca_shrink` `PROVEN`), P6.1, P6.2 and P6.4a all pass. One such session is enough; a session with `UNPROVEN` or `FAILED` is not a pass. The report states that the deployment is a harness fixture and that live restart recovery was not proven (the synthetic restart tests are the gate).

## Abort and flatten

| Code | When | Do |
|---|---|---|
| A1 | the stack does not start or IB is down before arming | `./docker.sh -d`; nothing was armed; retry another day |
| A2 | a harness step fails, or anything looks wrong while positions are open | 1. `mmr experiment pause --reason "abort"` (no new AI entries). 2. `mmr flatten --reason "acceptance abort" --wait` → must print `FLAT`. 3. `mmr reconcile`. 4. `mmr experiment stop --reason "aborted"`. 5. Keep the evidence. |
| A3 | the session flatten did not reach flat by 15:55, or `stop` says `NOT_FLAT` | `mmr flatten --reason "flatten missed" --wait`; if it does not print `FLAT` within 5 minutes or the trader is unreachable: close the positions by hand in IB Client Portal or TWS with the paper login (IB Gateway has no order screen), cancel every working order there, then `mmr reconcile` and `mmr experiment stop`. A tripped breaker stays tripped until you clear it after a review. |
| A4 | `OCA_SIBLING_NOT_SHRUNK`: IB did not shrink the sibling after a partial fill (a possible oversell risk) | 1. `mmr experiment pause --reason "oca shrink failed"`. 2. `mmr flatten --reason "oca shrink failed" --wait` → must print `FLAT`. 3. In Client Portal cancel any working S stop or target. 4. `mmr reconcile`; `mmr experiment stop --reason "aborted"`. 5. Keep the evidence and report it as a defect in the Plan 1 OCA design; SP1 is not accepted. |

Never re-run `mmr experiment acceptance run` with a new run id after a failure
on the same day; resume with `--run-id` only if the failure was the host
process (resume validates the account against the journal and replays the stored request bodies unchanged; it sends no new order).
````

- [ ] **Step 4: Run** the parse tests, then the full suite.
- [ ] **Step 5: Commit** — `docs: add the sp1 paper acceptance runbook`.

---

## Self-review against the spec

| Spec requirement | Task |
|---|---|
| A green suite is not a paper result; one real IB paper session before SP2 trades | 8 (runbook P0–P6) |
| `mmr experiment acceptance`, run by the operator | 5 |
| Registers a catalogue strategy with `register_ai_deployment`, signed by `ai_research`, seal as in production | 2, 3 |
| Publishes a policy and submits decisions with `ai_supervisor`: two entries, partial then full close of the first, second left for the session flatten | 2, 3 |
| Passes only if the flatten reaches `FLAT` on broker evidence | 2 (`finish`), 3, 4 |
| Real methods and keys; never seeds the database; never skips the seal | 2 (import test), 3 (`test_the_harness_seeds_nothing`) |
| Runs on the host as the operator; keys from `~/.config/mmr/keys/rpc/`; no container gets them | 5 |
| Refuses unless paper and an experiment is `ARMED` | 2, 5 |
| Not an autonomous loop | 2 (fixed sequence, no decider) |
| Every section 6 item mapped to an owner | map above (no NO OWNER row left; live OCA shrink → Task 7 / P4S) |
| Spec 5.1: the real paper session proves the partial-fill shrink | 7 (ruling 13), runbook P4S |
| #34 decided: no reset by default; clean-account gate with exact commands | ruling 3, 2, 5, 7 (P2) |

## Owner questions

None open. The six questions were answered by the owner on 2026-10-06 and are rulings 17–22 (sizes, Telegram, live restart, signing key, one session, harness fixture); the OCA proof question is ruling 13.
