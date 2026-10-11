# AI Paper SP3 — Plan 3: Trader authority for AI-written strategies — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** The trader is the authority over authored strategies. A new principal `ai_author` stores one authored file per call in a content-addressed store and reads its own records and the cap. The trader keeps one record per digest with the spec 7.3 states, lets an authored key into SP2c evaluation claims only while the owner switch is on, moves the record on research reports, Jev judgments, registration, retirement and expiry, refuses an authored registration until an isolation proof passed for the running sandbox image, and refuses authored entries (never closes) while the switch is off.

**Architecture:** Two journal tables (migration 130 `authored_strategies`, 131 `authored_isolation_proofs`) behind `AuthoredStrategyStore` and `IsolationProofs` in `trader/automation/`. The store writes `<hex>.py` and `<hex>.json` to the `mmr_authored_strategies` volume (temp file, fsync, atomic rename, file before record), then the record in one `DuckDBConnection.transaction`, where the cap is counted. The SP2c pieces get small ports instead of new paths: `EvaluationClaims` asks the store inside its claim transaction, `BacktestJudgments` calls a hook inside its insert transaction, `AiDeploymentRegistrar` checks a gate before and marks the record inside its seal transaction, and `DeploymentActivity.entry_refusal` (admission and final dispatch) refuses an authored ENTER. Authored keys enter SP2c as the strategy path `authored:sha256:<hex>`, so `strategy_key(path, class)` is exactly the authored key. Seven direct typed-RPC handlers in `trader/messaging/authored_surface.py` re-check the caller. The host command `mmr ai-author prove-isolation` runs `mmr ai-author prove-isolation --in-container` inside the `research` container (Plan 2's checks over the research socket), checks the printed JSON and records it as `cli`; no `docker inspect` (decisions D2, D3).

**Tech Stack:** Python 3.12, DuckDB (`SchemaMigrator`, `DuckDBConnection.transaction`), pydantic v2 strict wire models, `ast` (tunable constants), Ed25519 typed RPC, `exchange_calendars` through `XNYSCalendarPolicy`, pytest. No new dependency.

**Spec:** `docs/superpowers/specs/2026-10-11-ai-paper-sp3-sandboxed-codegen-design.md`, sections 5, 6 (trader part), 7.3, 7.9, 8 and the section 10 bullets *Authority*, *Keys*, *Config* (trader.yaml), *Cap* and *Staged deploy*. Index: `docs/superpowers/plans/2026-10-11-ai-paper-sp3-00-index.md`.

## Global Constraints

- **Base:** master at `d69fdcf2` or later, **with SP3 Plan 1 merged** (`trader/authored/keys.py`, `trader/authored/static_check.py`). Task 10 also needs **SP3 Plan 2 merged** (`trader/authored/sandbox_client.py`, `trader/sandbox/isolation_checks.py`, `trader/sandbox/protocol.py`); Tasks 1-9 do not.
- **Journal migrations:** this plan uses **130** (`authored_strategies`) and **131** (`authored_isolation_proofs`). 132 is Plan 5's (`ai_costs.role` gains `author`, owner ruling 2026-10-11); 133-134 stay free. Check both are free on the base (`grep -rn "13[01]" trader/automation trader/data/schema_migrations.py`). New tables only, no ALTER.
- **Words** from spec section 3, exactly: authored strategy, catalogue strategy, store, digest, session, authored key (`authored:sha256:<hex>:<Class>`).
- **Names owned here (index rule R0):** principal `ai_author`; methods `submit_authored_strategy`, `get_authored_strategies` (`ai_author`), `report_authored_evaluation` (`research`), `retire_authored_strategy` (`strategy`), `record_isolation_proof` (`cli`), `get_isolation_proof` (`cli`, `strategy`), `list_authored_strategies` (`cli`, `dashboard`); `AuthoredState`, `COUNTED_STATES`, `AuthoredStrategyStore` in `trader/automation/authored_strategies.py`; config `ai_paper.authored_strategies`; refusal codes `AUTHORED_STRATEGIES_DISABLED`, `AUTHORED_CAP_FULL`, `AUTHORED_STATIC_REJECTED`, `AUTHORED_BAD_TUNABLES`, `AUTHORED_BAD_BAR_SIZE`, `AUTHORED_ISOLATION_UNPROVEN`, plus `AUTHORED_STATE_CONFLICT` and `AUTHORED_STRATEGY_UNKNOWN`; CLI `mmr ai-author list` and `mmr ai-author prove-isolation`.
- **Names used from Plan 1, exactly:** `trader.authored.keys.source_digest(source: bytes) -> str`, `authored_key(digest, class_name) -> str`, `parse_authored_key(key) -> tuple[str, str]` (raises `ValueError`), `is_authored_key(key) -> bool`; `trader.authored.static_check.require_clean(source: str) -> str`, `AuthoredStaticRejected` (attribute `violations`: `StaticViolation(rule, line, detail)`).
- **Names used from Plan 2, exactly (Task 10 only):** `SandboxClient(socket_path)`, `SandboxUnavailable`, `run_isolation_checks(client) -> IsolationResult(passed, image_digest, checks)`, `RESEARCH_SOCKET`, `BUILD_ID_RE`. Each `CheckResult` is read as `name`, `passed`, `detail` (see Cross-plan additions).
- **Wire models:** `ConfigDict(extra="forbid", strict=True, frozen=True)`. Every key is required; optional values are explicit `null`. Times are ISO-8601 text with an offset. `True` is never a number.
- **Refusals:** a business refusal is a reply body `{"status": "REFUSED", "code", "detail", "retryable", ...}`, never an RPC error. A wrong caller is `PERMISSION_DENIED`; a malformed body `VALIDATION_ERROR`; an unusable store directory is the RPC error `AUTHORED_STORE_UNAVAILABLE` (fail loudly).
- **No controller epoch, no command ledger** on the seven methods. They record facts; idempotency rests on the digest, the request id and the proof id.
- **DuckDB:** only `DuckDBConnection.execute` / `.transaction`. Inside a transaction callback never call `db.execute` or `db.transaction` (non-reentrant lock). File writes, the static check and reads of other stores run before the transaction.
- **Config:** `ai_paper.authored_strategies` in `trader.yaml` only; `AI_PAPER*` env names stay refused by `_refuse_env_overrides`. No RPC method writes it. A trader restart applies an edit.
- **Paper only:** the surface registers only when `ai_paper.enabled` built the services, and that build refuses a live account (`AI_PAPER_LIVE_REFUSED`).
- **No AI code runs in the trader.** The trader parses the authored file with `ast` and Plan 1's checker only; it never imports or executes it.
- **Never print secrets.** No key bytes, no tokens, no `.env` values in messages, logs or the isolation record.
- Test-first. Per task run only the listed tests. Full suite once, in Task 11.
- Commit subjects `feat:` / `test:` / `docs:` / `chore:`, lowercase, imperative. Every commit ends with a blank line and `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.
- No container restart, Docker run, broker order, deploy, push or GitHub post is authorized by this plan.

## Rulings

Each ruling is one line; the owner confirms them in the index under "Owner rulings to confirm".

1. **Plan 3 ruling 1:** An authored key enters SP2c with the strategy path `authored:sha256:<hex>`, so `strategy_key(path, class)` is the authored key; `is_strategy_key` and `strategy_allowlist` stay catalogue only, and `is_evaluation_key` / `split_evaluation_key` widen the request body, the case, the judgment binding, the judgment port, the renewal check and `AiDeployment.strategy_path`.
2. **Plan 3 ruling 2:** A case whose key is authored must have `strategy_file_hash` equal to the key's digest (`CASE_MALFORMED` otherwise), so a case can never bind one file's key to other bytes.
3. **Plan 3 ruling 3:** The trader never reads `ai.yaml`; its bar-size list is `ai_paper.authored_strategies.bar_sizes` (default `1 min`, `5 mins`, `15 mins`, each passing `check_bar_size`), and Plan 5 offers the author only sizes on both lists.
4. **Plan 3 ruling 4:** `max_active` is an integer 1-3 (controller decision C2: the sandbox live pool has 3 sessions, one per authored deployment, so a larger cap could deploy a strategy that never gets a live session), and `authored_strategies.enabled: true` needs `ai_paper.enabled: true` (a loud load error, not a silent no-op).
5. **Plan 3 ruling 5 (SP3 decision D5):** Tunables on the wire are `{NAME: {"low": n, "high": n, "default": n}}` (1-32: SP2c needs one to vary for its neighbour points); each name is an upper-case numeric constant of the class (never `LOOKBACK_BARS`) whose class value lies in `[low, high]`, and `default` must equal that class value, so the two spellings cannot disagree. The record stores all three; research's cohort point 0 and the claim use the stored default.
6. **Plan 3 ruling 6:** An existing digest returns `EXISTING` with the stored record before the switch, cap and static checks, so a retry after a lost reply never sees `AUTHORED_CAP_FULL`; the stored tunables and note win over a resubmit's.
7. **Plan 3 ruling 7:** An authored key is claimable only while the switch is on and its record is `STORED`, with the record's class and bar size and every cohort value inside its range (`STRATEGY_NOT_ALLOWED` otherwise); the accepted claim moves the record to `EVALUATING` in the claim's own transaction, so a refused claim leaves it `STORED`.
8. **Plan 3 ruling 8:** Switch off refuses new submits, claims, registrations and entries; an evaluation already claimed still reports and is judged, so no record is stranded in `EVALUATING` ("states unchanged", spec 8).
9. **Plan 3 ruling 9:** An INITIAL judgment of an authored key moves the record in the judgment's transaction: REJECT → `REJECTED JEV_REJECT`, SHADOW → `SHADOW JEV_SHADOW`, DEPLOY → `SHADOW AWAITING_REGISTRATION`, NO_VERDICT → `FAILED JEV_NO_VERDICT`; a judgment for a record that is not `EVALUATING` on that request is refused `AUTHORED_STATE_CONFLICT`.
10. **Plan 3 ruling 10:** `SHADOW → DEPLOYED` is allowed only for a DEPLOY verdict, at registration, in the seal transaction; spec 8 needs it although the 7.3 table lists only `RETIRED` and `EXPIRED` after `SHADOW`.
11. **Plan 3 ruling 11:** Expiry is computed lazily on every submit and list: `SHADOW` expires after `backtest_judge.deploy_expiry_sessions` XNYS sessions; `DEPLOYED` follows its version line (WITHDRAWN → `RETIRED OWNER_RETIRED`, EXPIRED → `EXPIRED DEPLOY_EXPIRED`, judgment ended → `EXPIRED RENEWAL_ENDED`); a calendar error leaves the record counted.
12. **Plan 3 ruling 12 (controller decision C1; review r1 #5):** The identity is Plan 2's build id (decision D2). The trader cannot reach the sandbox, so "the image the sandbox reports" is the build id in research's latest `STARTED` report (research reads it from its own `PING`); registration needs the latest proof for that build id to pass. Decision D2 asked not to key on the `STARTED` report, but the trader has no other view of the running sandbox and spec 10 "staged deploy" needs a rebuild to block registration, so the channel stays. Registration needs a passing newest proof for both the build that evaluated the record (its own `STARTED`) and the build research reported last, checked before the seal and again inside the seal transaction, so a failed run recorded meanwhile stops it. Registration does not claim the build running now is proven: the binding check is the strategy service's, which opens no live session unless the build that session reports in `OPENED` has a passing proof (Plan 4 ruling 19). A sandbox rebuilt after the last `STARTED` therefore registers on old evidence but never trades until proven.
13. **Plan 3 ruling 13:** `get_isolation_proof` is readable by `cli` and `strategy` (the index said `cli`), because spec 7.9 makes the strategy service refuse to load without a proof.
14. **Plan 3 ruling 14:** `mmr ai-author list` reads a new `list_authored_strategies` (`cli`, `dashboard`) with the same reply as `get_authored_strategies`, which stays `ai_author` only as spec 5 says.
15. **Plan 3 ruling 15 (decision D3; owner ruling 2026-10-11):** The in-container step refuses (`LIVE_SESSIONS_OPEN`, exit 3, nothing printed) while the sandbox reports an open live session before or after the run, so no hostile probe runs next to a live strategy's child (Plan 2 rulings 21, 29). `prove-isolation` runs `<compose> exec -T research mmr ai-author prove-isolation --in-container` (research socket and pool, never a live slot). The in-container step builds no SDK and needs no key: it runs Plan 2's `run_isolation_checks` (posture from `PONG` incl. caps, seccomp, no-new-privileges, network, env, read-only root and pids 64; a benign session; every hostile escape) and prints the proof JSON. The host step checks it with `RecordIsolationProofRequest` and records it as `cli`. No `docker inspect` (decision D2).
16. **Plan 3 ruling 16 (review r1 #4):** The entry gate is in `DeploymentActivity.entry_refusal`, which both admission and final dispatch call: switch off → `AUTHORED_STRATEGIES_DISABLED`, record not `DEPLOYED` → `DEPLOYMENT_NOT_ACTIVE`; `CLOSE` and `PARTIAL_CLOSE` never reach it; `DeploymentActivity.active()` (what `get_active_ai_deployments` gives the strategy service) leaves out an authored deployment the gate would refuse, so a retired one is not reloaded even before its withdrawal lands (ruling 18). The last check is `send_gate_in_tx` on the saga's SUBMITTING transaction (added to `_ai_paper_send_gate_in_tx`): a retirement or switch-off that commits before the SUBMITTING row refuses the send; one that commits after it finds the entry already in flight, as an SP2c withdrawal does.
17. **Plan 3 ruling 17:** A research report must name the record's current request id; a repeat of the same event and request id is `UNCHANGED`, so a retried `SANDBOX_UNAVAILABLE` counts one attempt; the third attempt is `FAILED SANDBOX_UNAVAILABLE`.
18. **Plan 3 ruling 18 (decision D4):** `retire_authored_strategy {digest, code: SANDBOX_ERROR|SANDBOX_DIGEST_MISMATCH, detail}` moves `DEPLOYED → RETIRED` with reason `SANDBOX_ERROR` (spec 8 row "digest mismatch"; the code stays in the history), then withdraws the deployment version through the SP2c path (`AiDeploymentVersionStore.withdraw`, reason `AUTHORED_SANDBOX_ERROR`, principal `strategy`, command id `authored-retire:<version>`). A withdrawal refused `WITHDRAWAL_ENTRY_IN_FLIGHT` replies `REFUSED` with `retryable: true` and the record already `RETIRED`; a repeat re-checks the withdrawal and is `UNCHANGED`.
19. **Plan 3 ruling 19:** For spec 7.7, `judged_count` is the number of authored records with a recorded judgment (any verdict), returned by `report_authored_evaluation` and both list reads; Plan 4 adds the file's own trials.
20. **Plan 3 ruling 20:** The trader mounts `mmr_authored_strategies` read-write at `/store`, as Plan 2 ruling 22 asks (`MMR_AUTHORED_STORE_DIR=/store` in compose; a host run defaults to `~/.local/share/mmr/authored_strategies`); the image's `/store` is owned by `trader`; files are 0644; whichever of Plans 2 and 3 merges second keeps one Dockerfile line and one top-level volume entry.
21. **Plan 3 ruling 21:** Isolation records are insert-only and not sealed (no secret, a `cli`-only writer); `proof_id` is the digest of the body, so a resend is `EXISTING`; `passed` must equal "every check passed".
22. **Plan 3 ruling 22 (decision D2):** The sandbox "image digest" is Plan 2's build id (Plan 2 ruling 1: `PONG.image_digest`, read from `/etc/mmr-build-id` baked into the image), text matching `^[A-Za-z0-9][A-Za-z0-9._:+-]{0,127}$` (`IMAGE_ID`, the same text as Plan 2's `BUILD_ID_RE`) and never `unset`. One spelling everywhere: the `STARTED` report, the proof, `get_isolation_proof` and the strategy load check.
23. **Plan 3 ruling 23:** The operator runs `./docker.sh -k` before deploying this plan (new `ai_author` pair); `docker.sh` already refuses to start while a bound key file is missing.

## Cross-plan additions

Plans 4 and 5 use these exact names. Plan 2 must provide the two items marked **Plan 2**.

**Principal and keys.** `KNOWN_PRINCIPALS` and `CLIENT_PRINCIPALS` gain `ai_author`; `CALLS["ai_author"] = {"trader"}`; `SERVER_ACCEPTS["trader"]` gains it; `SERVICE_EXTRA_PRINCIPALS["ai"] = ("ai_research", "ai_author")`; `RESTART_ON_ROTATE["ai_author"] == ("ai", "trader")`. The `ai` service mounts `ai_author.key` and `ai_author.pub`, the trader `ai_author.pub`.

**Keys (`trader/research/strategy_key.py`):** `AUTHORED_PATH` (regex `^authored:sha256:[0-9a-f]{64}$`), `is_authored_path(value) -> bool`, `authored_strategy_path(digest) -> str`, `authored_digest_of_path(path) -> str`, `is_evaluation_key(value) -> bool`, `split_evaluation_key(key) -> tuple[str, str]`. Plan 4 replaces the research-side `split_strategy_key` calls (`trader/research/cohort.py`, `trader/research/case_builder.py`) with `split_evaluation_key`.

**Wire (`trader/automation/authored_wire.py`):**
- `submit_authored_strategy` `{source: str (1-20480 chars), tunables: {NAME: {low: number, high: number, default: number}} (1-32; default equals the class constant, ruling 5), bar_size: str, note: str (≤ 600), author_model: ^[A-Za-z0-9_./:@+-]{1,128}$}` → `{"status": "STORED"|"EXISTING"|"REFUSED", "code", "detail", "retryable", "record": RecordView|null}`. Codes: `AUTHORED_STRATEGIES_DISABLED`, `AUTHORED_CAP_FULL`, `AUTHORED_STATIC_REJECTED` (detail lists `rule@line: detail`), `AUTHORED_BAD_BAR_SIZE`, `AUTHORED_BAD_TUNABLES`.
- `get_authored_strategies` `{}` and `list_authored_strategies` `{}` → `{"enabled": bool, "max_active": int, "counted": int, "room": int, "judged_count": int, "bar_sizes": [str], "records": [RecordView]}` (oldest first).
- `report_authored_evaluation` `{digest, request_id: sha256 (the SP2c claim id), event: "STARTED"|"SANDBOX_UNAVAILABLE"|"SANDBOX_ERROR", sandbox_image_digest: build id|null (required for STARTED, else null; ruling 22), detail: str (≤ 500)}` → `{"status": "UPDATED"|"UNCHANGED"|"REFUSED", "code", "detail", "retryable", "record", "judged_count"}`. Codes: `AUTHORED_STRATEGY_UNKNOWN`, `AUTHORED_STATE_CONFLICT`.
- `retire_authored_strategy` `{digest, code: "SANDBOX_ERROR"|"SANDBOX_DIGEST_MISMATCH", detail}` → `{"status": "UPDATED"|"UNCHANGED"|"REFUSED", "code", "detail", "retryable", "record"}`. It also withdraws the deployment version (ruling 18); `WITHDRAWAL_ENTRY_IN_FLIGHT` is `REFUSED` with `retryable: true` and the record `RETIRED`: resend.
- `record_isolation_proof` `{image_digest: build id, passed: bool, checks: [{name: ^[a-z0-9_.:-]{1,64}$, passed: bool, detail: str ≤ 500}] (1-64, distinct names), ran_at}` → `{"status": "RECORDED"|"EXISTING", "proof": ProofView}`.
- `get_isolation_proof` `{image_digest: build id|null}` → `{"found": bool, "proof": ProofView|null}` (the latest proof for that image, or the latest of all when null).
- `RecordView` = `{digest, key, class_name, bar_size, tunables: {NAME: {low, high, default}}, note, author_model, state, reason, attempts, evaluation_request_id, sandbox_image_digest, verdict, judgment_id, deployment_version, created_at, state_changed_at, history}`. `ProofView` = `{proof_id, image_digest, passed, checks, ran_at, recorded_at}`.

**What Plans 4 and 5 do with them.**
- **Plan 4 research:** after `claim_evaluation` is ACCEPTED for an authored key, report `STARTED` with the claim's request id and `SandboxClient.ping().image_digest` (the build id); an availability error before the holdout reports `SANDBOX_UNAVAILABLE`; a smoke-stage `SANDBOX_ERROR` reports `SANDBOX_ERROR`; the case's `strategy_trials` uses `judged_count`. The reply's `record` carries the class, bar size and the tunables with ranges and defaults.
- **Plan 4 strategy:** on a live `SANDBOX_ERROR` or a digest mismatch call `retire_authored_strategy` and resend while the reply is `REFUSED` with `retryable: true`; before loading an authored deployment call `get_isolation_proof {"image_digest": <live PONG build id>}` and refuse unless `found` and `passed`. `get_active_ai_deployments` rows may carry an authored path (Task 3 Step 7).
- **Plan 5 ai:** as `ai_author`, read `get_authored_strategies` (switch, room, bar sizes, earlier outcomes) and call `submit_authored_strategy` with `{low, high, default}` per tunable (default = the class constant, read with `ast`). The research cycle offers a `STORED` record at cohort point 0 = its stored defaults. The ai research cycle registers a record in `SHADOW` with reason `AWAITING_REGISTRATION` again after a proof passes (registration is refused `AUTHORED_ISOLATION_UNPROVEN` until then).
- **Plan 2 (already in its plan):** (a) `PONG`'s `image_digest` is the build id from `/etc/mmr-build-id` (Plan 2 ruling 1; this plan's ruling 22), validated by `BUILD_ID_RE`; (b) each `CheckResult` has `name`, `passed: bool` and `detail: str`; this plan also needs `name` to match `^[a-z0-9_.:-]{1,64}$`; (c) the image creates `/store` owned by `trader` (Plan 2 ruling 22); (d) the posture covers the read-only root and the pids limit, so no host `docker inspect` is needed (Plan 2 ruling 27).

**Differences from Plans 4 and 5 as drafted on 2026-10-11 (index rule R0: these shapes win; they adapt):**
- Plan 4 R1/R2 expect `is_strategy_key` to accept authored keys and the path `authored/<hex>.py`. Here the allowlist key check stays catalogue only, the path is `authored:sha256:<hex>`, and callers use `is_evaluation_key` / `split_evaluation_key` (ruling 1).
- Plan 4 R4 expects `report_authored_evaluation {request_id, digest, outcome, reason, detail}`. Here it is `{digest, request_id, event, sandbox_image_digest, detail}` with `STARTED` as a required first event: the trader learns the running build id only from it (ruling 12).
- Plan 4 R6 expects `retire_authored_strategy {digest, reason, detail}` and a withdrawal. Here it is `{digest, code, detail}`, and the version is withdrawn through the SP2c path (ruling 18, decision D4); `get_active_ai_deployments` also stops listing a retired authored deployment at once (ruling 16).
- Plan 4 Task 1 widened `ActiveAiDeployment.strategy_path`. That change now lives here (Task 3 Step 7, decision D4).
- Plan 5 expects `submit_authored_strategy` with `class_name` and tunables `{default, min, max}`, and a `strategies` list. Here the class comes from the static check, tunables are `{low, high, default}` with `default` equal to the class constant (ruling 5, decision D5), and the list is `records` with `key`.
- Plan 4 R3 sent `sha256:` image digests. There is one spelling: the build id `PONG` reports (ruling 22, decision D2).

## Review Focus

1. **Two submits race for the last cap slot.** Exactly one is `STORED`, the other `AUTHORED_CAP_FULL`, and the counted rows equal `max_active`. → Task 5 `test_two_submits_for_the_last_slot_store_exactly_one`.
2. **A reply is lost after the submit committed.** The retry returns `EXISTING` with the same record, even when the cap is now full and the switch was turned off. → Task 5 `test_a_retry_after_a_lost_reply_is_existing_even_with_a_full_cap_and_the_switch_off`.
3. **The trader dies between the file write and the record.** Only an orphan file is left; the next submit of the same digest records it; a `<hex>.py` that holds other bytes is refused loudly, never overwritten. → Task 4 `test_a_file_with_other_bytes_under_the_digest_is_refused`; Task 5 `test_a_crash_after_the_file_leaves_an_orphan_that_the_next_submit_records`.
4. **Research retries a `SANDBOX_UNAVAILABLE` report after a lost reply.** It counts one attempt; three different nights make the record `FAILED SANDBOX_UNAVAILABLE`, the cap has room again and a new submit is accepted. → Task 6 `test_a_repeated_unavailable_report_counts_once`, `test_three_unavailable_nights_fail_the_record_and_free_the_cap`.
5. **An authored claim is refused late (daily limit) or a judgment arrives for the wrong request.** The record stays `STORED` (the claim transaction rolls back) and nothing is recorded for the stray judgment. → Task 7 `test_a_claim_refused_by_the_daily_limit_leaves_the_record_stored`, `test_a_judgment_for_a_record_not_evaluating_on_that_request_records_nothing`.

## File map

| File | Responsibility | Task |
|---|---|---|
| `trader/messaging/principals.py`, `trader/messaging/rpc_keys.py`, `docker-compose.yml` | `ai_author` principal, ACL rows, key mounts | 1 |
| `trader/automation/authored_config.py`, `trader/automation/ai_paper_config.py`, `config_defaults/trader.yaml` | the owner block | 2 |
| `trader/research/strategy_key.py`, `trader/research/evaluation_request.py`, `trader/research/evaluation_case.py`, `trader/automation/backtest_judgments.py` (binding), `trader/automation/ai_judgment_port.py`, `trader/automation/deployment_renewal.py`, `trader/automation/ai_deployments.py`, `trader/messaging/ai_deployment_wire.py` | authored keys inside SP2c; the authored path in the active-deployment reply (D4) | 3 |
| `trader/automation/authored_schema.py`, `trader/automation/authored_files.py`, `trader/automation/authored_strategies.py` (core), `trader/data/schema_migrations.py` (docstring), `trader/trading/command_stack.py` (migrations), `docker-compose.yml`, `Dockerfile`, `docker.sh` | tables 130-131, the file store, states | 4 |
| `trader/automation/authored_wire.py`, `trader/automation/authored_submission.py`, `trader/automation/authored_strategies.py` (submit, overview) | `submit_authored_strategy` | 5 |
| `trader/automation/authored_strategies.py` (report, retire with the SP2c withdrawal, refresh, `DeploymentLines`) | research and strategy reports, expiry | 6 |
| `trader/automation/evaluation_claims.py`, `trader/automation/backtest_judgments.py`, `trader/automation/authored_strategies.py` (claim, judgment hooks) | SP2c claims and judgments | 7 |
| `trader/automation/authored_isolation.py`, `trader/automation/ai_deployment_registration.py`, `trader/automation/ai_deployment_activity.py`, `trader/automation/authored_strategies.py` (gates) | proofs, staged deploy, entry gate | 8 |
| `trader/messaging/authored_surface.py`, `trader/messaging/production_api.py`, `trader/trading/command_stack.py` | the seven methods, wiring | 9 |
| `trader/authored/prove_isolation.py`, `trader/authored/isolation_host.py`, `trader/sdk.py`, `trader/mmr_cli.py`, `docs/CLI_REFERENCE.md`, `AGENTS.md`, `tests/fullstack/test_prove_isolation_in_research.py` | `mmr ai-author` (incl. `--in-container`, D3), docs | 10 |
| — | full suite | 11 |

Shared test helpers: `tests/automation/authored_fixtures.py` (created in Task 4, extended in Tasks 5-8).

---
### Task 1: The `ai_author` principal, its rights and its key mounts

**Files:**
- Modify: `trader/messaging/principals.py`, `trader/messaging/rpc_keys.py`, `docker-compose.yml`
- Modify (tests that pin exact sets): `tests/test_compose_ai_service.py`, `tests/test_research_principal.py`
- Test: `tests/test_ai_author_principal.py`

**Interfaces:**
- Consumes: `service_rpc_files`, `service_principals`, `init_keys`, `keys_cli.main(argv, in_container, out)`, `write_keyset`.
- Produces: principal `ai_author`; the seven `TRADER_ACL` rows listed in Cross-plan additions; `RESTART_ON_ROTATE["ai_author"] == ("ai", "trader")`.

- [ ] **Step 1: Write the failing tests** (`tests/test_ai_author_principal.py`)

```python
"""SP3 Plan 3 Task 1: the ai_author principal, its rights and its key mounts (spec 5, 10 Keys)."""
from __future__ import annotations

import io
import shutil
from pathlib import Path

import pytest

from tests.compose_rpc_helpers import ROOT, load_compose, visible_rpc_files
from tests.rpc_identity_fixtures import write_keyset
from trader.messaging.keys_cli import main as keys_main
from trader.messaging.principals import (
    CALLS, CLIENT_PRINCIPALS, KNOWN_PRINCIPALS, SERVER_ACCEPTS, SERVER_PRINCIPALS, SERVICE_PRINCIPAL, TRADER_ACL,
    service_principals, service_rpc_files,
)
from trader.messaging.rpc_keys import RESTART_ON_ROTATE, init_keys

AUTHORED_RIGHTS = {
    ("command", "submit_authored_strategy"): {"ai_author"},
    ("query", "get_authored_strategies"): {"ai_author"},
    ("query", "list_authored_strategies"): {"cli", "dashboard"},
    ("command", "report_authored_evaluation"): {"research"},
    ("command", "retire_authored_strategy"): {"strategy"},
    ("command", "record_isolation_proof"): {"cli"},
    ("query", "get_isolation_proof"): {"cli", "strategy"},
}


@pytest.fixture(autouse=True)
def rpc_keys_dir(tmp_path, monkeypatch):
    """check-mount loads keys; it must look at this test's keys, never the owner's."""
    monkeypatch.setenv("MMR_RPC_KEYS_DIR", str(tmp_path / "rpc"))


def test_ai_author_is_a_client_that_calls_only_the_trader():
    assert "ai_author" in KNOWN_PRINCIPALS and "ai_author" in CLIENT_PRINCIPALS
    assert "ai_author" not in SERVER_PRINCIPALS
    assert CALLS["ai_author"] == {"trader"}
    assert "ai_author" in SERVER_ACCEPTS["trader"]
    assert all("ai_author" not in accepted for server, accepted in SERVER_ACCEPTS.items() if server != "trader")


def test_authored_rights_are_exact():
    assert {key: set(TRADER_ACL[key]) for key in AUTHORED_RIGHTS} == AUTHORED_RIGHTS


def test_ai_author_holds_exactly_its_two_rights():
    rights = {key for key, allowed in TRADER_ACL.items() if "ai_author" in allowed}
    assert rights == {("command", "submit_authored_strategy"), ("query", "get_authored_strategies")}


def test_only_the_ai_service_holds_the_ai_author_private_key():
    assert service_principals("ai") == ("ai_supervisor", "ai_research", "ai_author")
    holders = {service for service in SERVICE_PRINCIPAL if "ai_author.key" in service_rpc_files(service)}
    assert holders == {"ai"}
    readers = {service for service in SERVICE_PRINCIPAL if "ai_author.pub" in service_rpc_files(service)}
    assert readers == {"ai", "trader"}


def test_compose_mounts_follow_the_principal_tables():
    services = load_compose()["services"]
    assert {"ai_author.key", "ai_author.pub"} <= visible_rpc_files(services["ai"])
    assert "ai_author.pub" in visible_rpc_files(services["trader"])
    for name, service in services.items():
        if name not in ("ai", "keygen"):
            assert "ai_author.key" not in (visible_rpc_files(service) or set()), name


def test_rotation_restarts_the_ai_service_and_the_trader():
    assert RESTART_ON_ROTATE["ai_author"] == ("ai", "trader")
    assert RESTART_ON_ROTATE["ai_research"] == ("ai", "research", "trader")
    assert RESTART_ON_ROTATE["trader"] == ("ai", "dashboard", "research", "strategy", "trader")


def test_keys_init_creates_the_ai_author_pair(tmp_path):
    rows = {row.principal: row.status for row in init_keys(tmp_path / "keys")}
    assert rows["ai_author"] == "created"
    assert (tmp_path / "keys" / "ai_author.key").stat().st_mode & 0o777 == 0o600


def _container_view(tmp_path: Path, service: str, *, extra: tuple[str, ...] = ()) -> list[str]:
    host = tmp_path / "host_rpc"
    write_keyset(host)
    rpc = tmp_path / "rpc"
    rpc.mkdir()
    for name in service_rpc_files(service) | set(extra):
        shutil.copy2(host / name, rpc / name)
    hmac_file = tmp_path / "service_hmac.key"
    hmac_file.write_bytes(b"")
    return ["check-mount", service, "--keys-dir", str(rpc), "--hmac-file", str(hmac_file)]


@pytest.mark.parametrize("service", ["trader", "strategy", "dashboard"])
def test_check_mount_fails_when_the_ai_author_key_is_mounted_outside_ai(tmp_path, service):
    out = io.StringIO()
    code = keys_main(_container_view(tmp_path, service, extra=("ai_author.key",)), in_container=lambda: True,
                     out=out)
    assert code == 1
    assert "unexpected ai_author.key" in out.getvalue()


def test_check_mount_passes_for_the_ai_service_with_its_three_pairs(tmp_path):
    out = io.StringIO()
    code = keys_main(_container_view(tmp_path, "ai"), in_container=lambda: True, out=out)
    assert code == 0, out.getvalue()


def test_the_key_check_gate_covers_the_ai_service_and_the_trader():
    line = next(line for line in (ROOT / "docker.sh").read_text().splitlines()
                if line.startswith("KEYCHECK_SERVICES="))
    assert {"ai", "trader"} <= set(line.split("=", 1)[1].strip('"').split())
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_ai_author_principal.py -q --timeout=60`
Expected: FAIL (`ai_author` is not a known principal; `KeyError: 'ai_author'`).

- [ ] **Step 3: Add the principal and its rows** (`trader/messaging/principals.py`)

Replace the principal sets and the trust matrix at the top of the file with:

```python
KNOWN_PRINCIPALS: frozenset[str] = frozenset({
    "trader", "strategy", "cli", "dashboard", "ai_supervisor", "ai_research", "research", "ai_author",
})
SERVER_PRINCIPALS: frozenset[str] = frozenset({"trader", "strategy", "research"})
# Principals the SDK may sign as (``MMR_RPC_PRINCIPAL``).
CLIENT_PRINCIPALS: frozenset[str] = frozenset({"cli", "ai_supervisor", "ai_research", "ai_author"})
```

```python
SERVER_ACCEPTS: Mapping[str, frozenset[str]] = {
    "trader": frozenset({"cli", "dashboard", "strategy", "ai_supervisor", "ai_research", "research",
                         "ai_author"}),
    "strategy": frozenset({"cli", "dashboard", "trader"}),
    # SP2c spec 5.1: like strategy, research is a server that also calls the trader.
    "research": frozenset({"ai_research", "cli"}),
}

CALLS: Mapping[str, frozenset[str]] = {
    "trader": frozenset({"strategy"}),
    "strategy": frozenset({"trader"}),
    "cli": frozenset({"trader", "strategy", "research"}),
    "dashboard": frozenset({"trader", "strategy"}),
    "ai_supervisor": frozenset({"trader"}),
    "ai_research": frozenset({"trader", "research"}),
    "research": frozenset({"trader"}),
    # SP3 spec 5: the author writes files through the trader only; it never reaches research or the sandbox.
    "ai_author": frozenset({"trader"}),
}
```

In `TRADER_ACL`, after the `("command", "record_shadow_result")` row, add:

```python
    # SP3 Plan 3 (spec 5, 7.3, 7.9): authored strategies. Explicit sets per method; each handler re-checks.
    ("command", "submit_authored_strategy"): frozenset({"ai_author"}),
    ("query", "get_authored_strategies"): frozenset({"ai_author"}),
    ("query", "list_authored_strategies"): HUMAN,                       # Plan 3 ruling 14
    ("command", "report_authored_evaluation"): frozenset({"research"}),
    ("command", "retire_authored_strategy"): frozenset({"strategy"}),
    ("command", "record_isolation_proof"): frozenset({"cli"}),
    ("query", "get_isolation_proof"): frozenset({"cli", "strategy"}),  # Plan 3 ruling 13
```

Replace `SERVICE_EXTRA_PRINCIPALS` and its comment with:

```python
# A service that signs as more than one principal. SP2 spec 4 and SP3 spec 5: the ai service holds the
# ai_supervisor, ai_research and ai_author keys (accepted limit: keys in one process are not isolation).
SERVICE_EXTRA_PRINCIPALS: Mapping[str, tuple[str, ...]] = {"ai": ("ai_research", "ai_author")}
```

- [ ] **Step 4: Derive the long-lived services' principals** (`trader/messaging/rpc_keys.py`)

Change the import line to:

```python
from trader.messaging.principals import (
    KNOWN_PRINCIPALS, is_valid_principal_name, peers_for, service_principals,
)
```

Replace the `_LONG_LIVED_SERVICE_PRINCIPALS` mapping (keep its comment) with:

```python
_LONG_LIVED_SERVICES = ("trader", "strategy", "dashboard", "ai", "research")
_LONG_LIVED_SERVICE_PRINCIPALS: Mapping[str, tuple[str, ...]] = {
    service: service_principals(service) for service in _LONG_LIVED_SERVICES
}
```

One table (`principals.SERVICE_EXTRA_PRINCIPALS`) now says which keys the `ai` service holds; a second hand-written copy would miss `ai_author` and drop `ai` from its rotation list.

- [ ] **Step 5: Mount the keys** (`docker-compose.yml`)

In the `trader` service, after the `ai_research.pub` bind, add:

```yaml
      - ${HOME}/.config/mmr/keys/rpc/ai_author.pub:/home/trader/.config/mmr/keys/rpc/ai_author.pub:ro
```

In the `ai` service, change the comment line `# Two RPC key pairs (ai_supervisor, ai_research) plus trader.pub. Its state, ai.duckdb, lives` to `# Three RPC key pairs (ai_supervisor, ai_research, ai_author) plus trader.pub and research.pub. Its state, ai.duckdb, lives`, and after the `ai_research.pub` bind add:

```yaml
      - ${HOME}/.config/mmr/keys/rpc/ai_author.key:/home/trader/.config/mmr/keys/rpc/ai_author.key:ro
      - ${HOME}/.config/mmr/keys/rpc/ai_author.pub:/home/trader/.config/mmr/keys/rpc/ai_author.pub:ro
```

No other service gets either file. `check-mount` then reports `unexpected ai_author.key` anywhere else, because `_mount_problems` compares each container with `service_rpc_files`.

- [ ] **Step 6: Update the two tests that pin exact sets**

`tests/test_compose_ai_service.py`: rename `test_ai_sees_its_two_key_pairs_and_the_trader_public_key_only` to `test_ai_sees_its_three_key_pairs_and_two_public_keys_only` and make its body:

```python
def test_ai_sees_its_three_key_pairs_and_two_public_keys_only(ai):
    assert visible_rpc_files(ai) == service_rpc_files("ai") == {
        "ai_supervisor.key", "ai_supervisor.pub", "ai_research.key", "ai_research.pub",
        "ai_author.key", "ai_author.pub", "trader.pub", "research.pub"}
```

`tests/test_research_principal.py`, in `test_research_has_no_trading_policy_or_decision_right`, replace the last two lines with:

```python
    assert rights == expected | {("command", "record_shadow_result"), ("command", "report_authored_evaluation")}
    assert TRADER_ACL[("command", "record_shadow_result")] == {"research"}
    assert TRADER_ACL[("command", "report_authored_evaluation")] == {"research"}
```

- [ ] **Step 7: Run the tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_ai_author_principal.py tests/test_compose_ai_service.py tests/test_compose_rpc_keys.py tests/test_keys_check_mount.py tests/test_rpc_keys_init.py tests/test_rpc_key_rotation.py tests/test_research_principal.py tests/test_rpc_acl.py tests/test_docker_helper.py -q --timeout=60`
Expected: PASS.

- [ ] **Step 8: Commit**

```bash
git add trader/messaging/principals.py trader/messaging/rpc_keys.py docker-compose.yml \
  tests/test_ai_author_principal.py tests/test_compose_ai_service.py tests/test_research_principal.py
git commit -m "feat: add the ai_author principal, its rights and its key mounts

The ai service signs as ai_author; only the trader accepts it. Run ./docker.sh -k
before deploying: the new key pair must exist (Plan 3 ruling 23).

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 2: The owner block `ai_paper.authored_strategies`

**Files:**
- Create: `trader/automation/authored_config.py`
- Modify: `trader/automation/ai_paper_config.py`, `config_defaults/trader.yaml`
- Test: `tests/automation/test_authored_config.py`

**Interfaces:**
- Consumes: `load_ai_paper_config(raw, *, trading_mode)`, `AiPaperConfigError`, `check_bar_size` (`trader/research/request_rules.py`).
- Produces: `AuthoredStrategiesConfig(enabled: bool = False, max_active: int = 3, bar_sizes: tuple[str, ...] = ("1 min", "5 mins", "15 mins"))`, `AuthoredStrategiesConfigError`, `load_authored_strategies_config(raw: object) -> AuthoredStrategiesConfig`, `AiPaperConfig.authored_strategies`.

- [ ] **Step 1: Write the failing tests** (`tests/automation/test_authored_config.py`)

```python
"""SP3 Plan 3 Task 2: the owner block ai_paper.authored_strategies (spec 6, 10 Config; rulings 3-4)."""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from trader.automation.ai_paper_config import AiPaperConfigError, load_ai_paper_config
from trader.automation.authored_config import AuthoredStrategiesConfig

ROOT = Path(__file__).resolve().parents[2]


def load(block):
    return load_ai_paper_config({"enabled": True, "authored_strategies": block}, trading_mode="paper")


def test_the_switch_is_off_by_default():
    config = load_ai_paper_config({}, trading_mode="paper").authored_strategies
    assert config == AuthoredStrategiesConfig(enabled=False, max_active=3, bar_sizes=("1 min", "5 mins", "15 mins"))


def test_the_block_loads():
    config = load({"enabled": True, "max_active": 2, "bar_sizes": ["5 mins"]}).authored_strategies
    assert (config.enabled, config.max_active, config.bar_sizes) == (True, 2, ("5 mins",))


@pytest.mark.parametrize("block, key", [
    ({"enabled": "yes"}, "authored_strategies.enabled"),
    ({"max_active": 0}, "authored_strategies.max_active"),
    ({"max_active": 4}, "authored_strategies.max_active"),       # C2: no more than the 3 live sandbox sessions
    ({"max_active": True}, "authored_strategies.max_active"),
    ({"bar_sizes": []}, "authored_strategies.bar_sizes"),
    ({"bar_sizes": "5 mins"}, "authored_strategies.bar_sizes"),
    ({"bar_sizes": ["1 hour"]}, "authored_strategies.bar_sizes"),
    ({"bar_sizes": ["fortnight"]}, "authored_strategies.bar_sizes"),
    ({"bar_sizes": ["5 mins", "5 mins"]}, "authored_strategies.bar_sizes"),
    ({"color": "red"}, "authored_strategies.color"),
    ("on", "authored_strategies"),
])
def test_a_bad_block_fails_load_and_names_the_key(block, key):
    with pytest.raises(AiPaperConfigError, match=key.replace(".", r"\.")):
        load(block)


def test_the_switch_needs_ai_paper_enabled():                                          # ruling 4
    with pytest.raises(AiPaperConfigError, match=r"requires ai_paper\.enabled"):
        load_ai_paper_config({"enabled": False, "authored_strategies": {"enabled": True}}, trading_mode="paper")


def test_an_environment_override_still_fails_load(monkeypatch):                        # spec 10 Config
    monkeypatch.setenv("AI_PAPER_AUTHORED_STRATEGIES_ENABLED", "true")
    with pytest.raises(AiPaperConfigError, match="environment override refused"):
        load({"enabled": False})


def test_the_shipped_template_loads_with_the_switch_off():
    raw = yaml.safe_load((ROOT / "config_defaults" / "trader.yaml").read_text())
    assert load_ai_paper_config(raw["ai_paper"], trading_mode="paper").authored_strategies == \
        AuthoredStrategiesConfig()
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/automation/test_authored_config.py -q --timeout=60`
Expected: FAIL (`ModuleNotFoundError: trader.automation.authored_config`).

- [ ] **Step 3: Write the block parser** (`trader/automation/authored_config.py`)

```python
"""``ai_paper.authored_strategies`` in trader.yaml (SP3 spec 6).

Operator only: ``load_ai_paper_config`` refuses every ``AI_PAPER*`` environment name and no RPC method
writes it. A trader restart applies an edit. The trader never reads ai.yaml, so the bar sizes an authored
file may use are listed here (Plan 3 ruling 3).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from trader.research.request_rules import check_bar_size

DEFAULT_BAR_SIZES = ("1 min", "5 mins", "15 mins")
# Controller decision C2: at most 3, the sandbox live pool (Plan 2 LIVE_POOL_SIZE: one session per deployment).
MAX_ACTIVE_RANGE = (1, 3)
_KEYS = ("enabled", "max_active", "bar_sizes")
_PREFIX = "ai_paper.authored_strategies"


class AuthoredStrategiesConfigError(ValueError):
    """The block is invalid; the message names the key path."""


@dataclass(frozen=True)
class AuthoredStrategiesConfig:
    enabled: bool = False
    max_active: int = 3
    bar_sizes: tuple[str, ...] = DEFAULT_BAR_SIZES


def load_authored_strategies_config(raw: object) -> AuthoredStrategiesConfig:
    if raw is None:
        return AuthoredStrategiesConfig()
    if not isinstance(raw, dict):
        raise AuthoredStrategiesConfigError(f"{_PREFIX} must be a mapping")
    unknown = sorted(set(raw) - set(_KEYS))
    if unknown:
        raise AuthoredStrategiesConfigError(f"{_PREFIX}.{unknown[0]}: unknown key")
    return AuthoredStrategiesConfig(enabled=_enabled(raw), max_active=_max_active(raw),
                                    bar_sizes=_bar_sizes(raw.get("bar_sizes", list(DEFAULT_BAR_SIZES))))


def _enabled(raw: Mapping[str, Any]) -> bool:
    value = raw.get("enabled", False)
    if type(value) is not bool:
        raise AuthoredStrategiesConfigError(f"{_PREFIX}.enabled must be true or false")
    return value


def _max_active(raw: Mapping[str, Any]) -> int:
    value = raw.get("max_active", 3)
    low, high = MAX_ACTIVE_RANGE
    if type(value) is not int or not low <= value <= high:
        raise AuthoredStrategiesConfigError(f"{_PREFIX}.max_active must be an integer from {low} to {high}")
    return value


def _bar_sizes(value: object) -> tuple[str, ...]:
    if not isinstance(value, list) or not value:
        raise AuthoredStrategiesConfigError(f"{_PREFIX}.bar_sizes must be a non-empty list")
    for size in value:
        if not isinstance(size, str):
            raise AuthoredStrategiesConfigError(f"{_PREFIX}.bar_sizes: {size!r} is not text")
        try:
            check_bar_size(size)
        except ValueError as exc:
            raise AuthoredStrategiesConfigError(f"{_PREFIX}.bar_sizes: {exc}") from None
    if len(set(value)) != len(value):
        raise AuthoredStrategiesConfigError(f"{_PREFIX}.bar_sizes must not repeat a bar size")
    return tuple(value)
```

- [ ] **Step 4: Accept the block in the ai_paper loader** (`trader/automation/ai_paper_config.py`)

Add the import under the `backtest_judge_config` import:

```python
from trader.automation.authored_config import (
    AuthoredStrategiesConfig,
    AuthoredStrategiesConfigError,
    load_authored_strategies_config,
)
```

Add `"authored_strategies"` to the end of `_PARSED_KEYS`:

```python
_PARSED_KEYS = (
    "enabled", "styles", "limits_ceiling", "experiment_kill_drawdown_pct", "experiment_kill_basis",
    "broker_outage_pause_seconds", "acceptance_probe", "model_budget_usd_per_day",
    "backtest_judge", "authored_strategies",
)
```

Add the field to `AiPaperConfig`, after `backtest_judge`:

```python
    # SP3 Plan 3 (spec 6): the owner switch and the cap for AI-written strategies; operator only.
    authored_strategies: AuthoredStrategiesConfig = field(default_factory=AuthoredStrategiesConfig)
```

In `load_ai_paper_config`, after `_check_drawdown_guard(ceiling, kill_pct)`, add:

```python
    authored = _parse_authored_strategies(raw.get("authored_strategies"))
    if authored.enabled and not enabled:
        raise AiPaperConfigError("ai_paper.authored_strategies.enabled: true requires ai_paper.enabled: true")
```

and pass `authored_strategies=authored,` to the `AiPaperConfig(...)` call, right after `backtest_judge=...`. Add the parser next to `_parse_backtest_judge`:

```python
def _parse_authored_strategies(value: object) -> AuthoredStrategiesConfig:
    try:
        return load_authored_strategies_config(value)
    except AuthoredStrategiesConfigError as exc:
        raise AiPaperConfigError(str(exc)) from None
```

- [ ] **Step 5: Ship the block in the template** (`config_defaults/trader.yaml`)

After the `strategy_allowlist: []` line of `backtest_judge`, add (two-space indent, inside `ai_paper`):

```yaml
  authored_strategies:               # SP3: AI-written strategies; operator only, no env override; restart the trader
    enabled: false                   # the owner switch: new authored files, their evaluations and their entries
    max_active: 3                    # authored strategies in STORED, EVALUATING, SHADOW or DEPLOYED (1-3)
    bar_sizes: [1 min, 5 mins, 15 mins]   # bar sizes an authored file may use; each at most 15 mins
```

- [ ] **Step 6: Run the tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/automation/test_authored_config.py tests/automation/test_backtest_judge_config.py tests/automation/test_ai_paper_config.py -q --timeout=60`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add trader/automation/authored_config.py trader/automation/ai_paper_config.py config_defaults/trader.yaml \
  tests/automation/test_authored_config.py
git commit -m "feat: accept the ai_paper.authored_strategies owner block

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 3: Authored keys inside SP2c

**Files:**
- Modify: `trader/research/strategy_key.py`, `trader/research/evaluation_request.py`, `trader/research/evaluation_case.py`, `trader/automation/backtest_judgments.py`, `trader/automation/ai_judgment_port.py`, `trader/automation/deployment_renewal.py`, `trader/automation/ai_deployments.py`, `trader/messaging/ai_deployment_wire.py` (`ActiveAiDeployment.strategy_path`; moved here from Plan 4 Task 1 by decision D4)
- Test: `tests/automation/test_authored_keys_in_sp2c.py`, `tests/strategy/test_authored_wire.py`

**Interfaces:**
- Consumes: Plan 1 `authored_key`, `parse_authored_key`, `is_authored_key`.
- Produces: `AUTHORED_PATH`, `is_authored_path(value: object) -> bool`, `authored_strategy_path(digest: str) -> str`, `authored_digest_of_path(path: str) -> str`, `is_evaluation_key(value: object) -> bool`, `split_evaluation_key(key: str) -> tuple[str, str]` in `trader/research/strategy_key.py`. `EvaluationRequestBody`, `EvaluationCase` and `AiDeployment` accept authored keys and paths. `trader.messaging.ai_deployment_wire.ACTIVE_STRATEGY_PATH` (a regex string matching either path); `ActiveAiDeployment` and `GetActiveAiDeploymentsResponse` accept an authored row (D4: the trader validates its own `get_active_ai_deployments` reply with this model, so without it one authored row breaks the reply for every deployment).

- [ ] **Step 1: Write the failing tests** (`tests/automation/test_authored_keys_in_sp2c.py`)

```python
"""SP3 Plan 3 Task 3: authored keys inside SP2c's request, case, binding and deployment (rulings 1-2)."""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from tests.automation.backtest_judge_fixtures import make_case, request_body
from trader.authored.keys import authored_key
from trader.automation.ai_deployments import AiDeployment, DeploymentRefused
from trader.automation.ai_judgment_port import strategy_key
from trader.automation.backtest_judge_config import BacktestJudgeConfigError, load_backtest_judge_config
from trader.automation.backtest_judgments import _binding
from trader.research.strategy_key import (
    authored_digest_of_path, authored_strategy_path, is_authored_path, is_evaluation_key, split_evaluation_key,
)
from trader.research.strategy_paths import normalize_strategy_path

DIGEST = "sha256:" + "c" * 64
AUTHORED = authored_key(DIGEST, "RangeBreak")
CATALOGUE = "strategies/opening_range_breakout.py:OpeningRangeBreakout"


def test_an_authored_key_splits_into_its_path_and_class_and_joins_back():
    path, class_name = split_evaluation_key(AUTHORED)
    assert (path, class_name) == ("authored:" + DIGEST, "RangeBreak")
    assert strategy_key(path, class_name) == AUTHORED
    assert is_authored_path(path) and authored_digest_of_path(path) == DIGEST
    assert split_evaluation_key(CATALOGUE) == ("strategies/opening_range_breakout.py", "OpeningRangeBreakout")
    assert is_evaluation_key(AUTHORED) and is_evaluation_key(CATALOGUE)


@pytest.mark.parametrize("value", ["authored:sha256:abc:X", "authored:" + DIGEST, "strategies/x.py", 7, None])
def test_anything_else_is_not_an_evaluation_key(value):
    assert not is_evaluation_key(value)
    if isinstance(value, str):
        with pytest.raises(ValueError):
            split_evaluation_key(value)


def test_the_allowlist_stays_catalogue_only():                                         # ruling 1
    with pytest.raises(BacktestJudgeConfigError):
        load_backtest_judge_config({"strategy_allowlist": [AUTHORED]})


def test_the_request_body_and_the_case_accept_an_authored_key():
    body = request_body(strategy_key=AUTHORED)
    assert body.strategy_key == AUTHORED
    assert make_case(body, strategy_file_hash=DIGEST).strategy_key == AUTHORED


def test_an_authored_case_must_hash_the_keys_own_file():                               # ruling 2
    with pytest.raises(ValidationError, match="strategy_file_hash must equal"):
        make_case(request_body(strategy_key=AUTHORED), strategy_file_hash="sha256:" + "d" * 64)


def test_the_judgment_binding_names_the_authored_path():
    case = make_case(request_body(strategy_key=AUTHORED), strategy_file_hash=DIGEST)
    binding = _binding(case, "sha256:" + "e" * 64)
    assert (binding["strategy_path"], binding["class_name"], binding["strategy_file_hash"]) == (
        "authored:" + DIGEST, "RangeBreak", DIGEST)


def deployment(**changes) -> AiDeployment:
    raw = {"strategy_path": authored_strategy_path(DIGEST), "strategy_digest": DIGEST, "class_name": "RangeBreak",
           "params": {"RANGE_BARS": 15}, "conids": [265598, 272093], "bar_size": "5 mins",
           "style": "intraday_long", "decider": "jev", "decider_verdict": "DEPLOY",
           "evidence_ref": "sha256:" + "1" * 64, "evidence_order_notional": 2000.0, **changes}
    return AiDeployment.from_json(raw)


def test_a_deployment_may_name_an_authored_path():
    assert deployment().strategy_path == "authored:" + DIGEST
    assert normalize_strategy_path(deployment().strategy_path) == "authored:" + DIGEST


@pytest.mark.parametrize("path", ["authored:sha256:abc", "authored/" + "c" * 64 + ".py",
                                  "authored:" + DIGEST + "/../x"])
def test_a_malformed_authored_path_is_invalid(path):
    with pytest.raises(DeploymentRefused) as refused:
        deployment(strategy_path=path)
    assert refused.value.code == "DEPLOYMENT_INVALID"
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/automation/test_authored_keys_in_sp2c.py -q --timeout=60`
Expected: FAIL (`ImportError: cannot import name 'authored_digest_of_path'`).

- [ ] **Step 3: Widen the key module** (`trader/research/strategy_key.py`, whole file)

This module now imports Plan 1's `trader/authored/keys.py`. That file must stay standard-library only (`hashlib`, `re`); if it imports anything under `trader.research`, this import is a cycle: move the shared regex into `keys.py` instead of importing back.

```python
"""The strategy key (SP2c spec 3): ``strategies/<file>.py:<Class>``, and SP3's authored key.

Cooldown, the trial count and holdout windows use it. It is not the registry's
``family_id``, which changes with every parameter set. An authored strategy (SP3 spec 3)
is keyed ``authored:sha256:<hex>:<Class>``; inside SP2c its strategy path is
``authored:sha256:<hex>``, so ``<path>:<Class>`` is again its key (Plan 3 ruling 1).
The allowlist stays catalogue only: ``is_strategy_key`` never accepts an authored key.
"""
from __future__ import annotations

import re

from trader.authored.keys import is_authored_key, parse_authored_key

STRATEGY_KEY = re.compile(r"^strategies/[A-Za-z0-9_]+(/[A-Za-z0-9_]+)*\.py:[A-Za-z_][A-Za-z0-9_]{0,63}$")
AUTHORED_PATH_PREFIX = "authored:"
AUTHORED_PATH = re.compile(r"^authored:sha256:[0-9a-f]{64}$")


def is_strategy_key(value: object) -> bool:
    return isinstance(value, str) and STRATEGY_KEY.fullmatch(value) is not None


def split_strategy_key(key: str) -> tuple[str, str]:
    if not is_strategy_key(key):
        raise ValueError(f"{key!r} is not strategies/<file>.py:<Class>")
    path, class_name = key.split(":", 1)
    return path, class_name


def is_authored_path(value: object) -> bool:
    return isinstance(value, str) and AUTHORED_PATH.fullmatch(value) is not None


def authored_strategy_path(digest: str) -> str:
    path = AUTHORED_PATH_PREFIX + str(digest)
    if not is_authored_path(path):
        raise ValueError(f"{digest!r} is not sha256:<64 lower-case hex>")
    return path


def authored_digest_of_path(path: str) -> str:
    if not is_authored_path(path):
        raise ValueError(f"{path!r} is not authored:sha256:<64 hex>")
    return path[len(AUTHORED_PATH_PREFIX):]


def is_evaluation_key(value: object) -> bool:
    """A key SP2c may evaluate: a catalogue strategy key or an authored key."""
    return is_strategy_key(value) or (isinstance(value, str) and is_authored_key(value))


def split_evaluation_key(key: str) -> tuple[str, str]:
    """(strategy path, class name) of either kind of key."""
    if is_strategy_key(key):
        return split_strategy_key(key)
    if isinstance(key, str) and is_authored_key(key):
        digest, class_name = parse_authored_key(key)
        return authored_strategy_path(digest), class_name
    raise ValueError(f"{key!r} is neither strategies/<file>.py:<Class> nor authored:sha256:<hex>:<Class>")
```

- [ ] **Step 4: Let the request body and the case carry an authored key**

`trader/research/evaluation_request.py`: replace `from trader.research.strategy_key import is_strategy_key` with `from trader.research.strategy_key import is_evaluation_key`, and the `_strategy_key` validator body with:

```python
        if not is_evaluation_key(value):
            raise ValueError("strategy_key must be strategies/<file>.py:<Class> or authored:sha256:<hex>:<Class>")
        return value
```

`trader/research/evaluation_case.py`: replace `from trader.research.strategy_key import is_strategy_key` with:

```python
from trader.authored.keys import is_authored_key, parse_authored_key
from trader.research.strategy_key import is_evaluation_key
```

and the first two lines of `_shape` (the `is_strategy_key` check) with:

```python
        if not is_evaluation_key(self.strategy_key):
            raise ValueError("strategy_key must be strategies/<file>.py:<Class> or authored:sha256:<hex>:<Class>")
        if is_authored_key(self.strategy_key) and parse_authored_key(self.strategy_key)[0] != self.strategy_file_hash:
            raise ValueError("strategy_file_hash must equal the authored key's digest")
```

- [ ] **Step 5: Split either kind of key where SP2c splits one**

In each of `trader/automation/backtest_judgments.py`, `trader/automation/ai_judgment_port.py` and `trader/automation/deployment_renewal.py`, replace the import `from trader.research.strategy_key import split_strategy_key` with `from trader.research.strategy_key import split_evaluation_key`, and every call `split_strategy_key(case.strategy_key)` in that file with `split_evaluation_key(case.strategy_key)` (one call in `_binding`, one in `Plan1Judgments.get`, one in `case_differences`). Run `grep -n split_strategy_key trader/automation` afterwards: no line may remain.

- [ ] **Step 6: Let a deployment name an authored file** (`trader/automation/ai_deployments.py`)

Add the import `from trader.research.strategy_key import is_authored_path`, add this validator after `_evidence_ref`:

```python
def _strategy_path(name: str, value: Any) -> str:
    """A catalogue file under strategies/, or an authored file named by its digest (SP3 Plan 3 ruling 1)."""
    if isinstance(value, str) and (_PATH.fullmatch(value) or is_authored_path(value)):
        return value
    raise _invalid(name, f"must match {_PATH.pattern} or ^authored:sha256:[0-9a-f]{{64}}$")
```

and in `_VALIDATORS` replace `"strategy_path": _matching(_PATH),` with `"strategy_path": _strategy_path,`.

- [ ] **Step 7: Let the active-deployment reply carry an authored row** (decision D4; was Plan 4 Task 1)

Write `tests/strategy/test_authored_wire.py`:

```python
"""SP3 D4: the trader validates its get_active_ai_deployments reply with this model, so an authored row
must pass it, and nothing else new may."""
import pytest
from pydantic import ValidationError

from trader.messaging.ai_deployment_wire import ActiveAiDeployment, GetActiveAiDeploymentsResponse
from trader.research.strategy_key import authored_strategy_path

DIGEST = "sha256:" + "c" * 64


def row(path, digest=DIGEST):
    return {"version_digest": "sha256:" + "d" * 64, "base_digest": "sha256:" + "b" * 64, "strategy_path": path,
            "strategy_digest": digest, "class_name": "OpeningRangeBreakout", "params": {}, "conids": [265598],
            "bar_size": "1 min", "expiry_session": "2026-11-06"}


def test_the_reply_carries_a_catalogue_and_an_authored_row():
    reply = GetActiveAiDeploymentsResponse.model_validate(
        {"account_mode": "paper",
         "deployments": [row("strategies/opening_range_breakout.py"), row(authored_strategy_path(DIGEST))]})
    assert [d.strategy_path for d in reply.deployments] == [
        "strategies/opening_range_breakout.py", authored_strategy_path(DIGEST)]


@pytest.mark.parametrize("path", ["authored:sha256:abc", "authored/" + "c" * 64 + ".py", "strategies/../x.py",
                                  "/etc/x.py", "authored:sha256:" + "C" * 64])
def test_other_paths_stay_refused(path):
    with pytest.raises(ValidationError):
        ActiveAiDeployment.model_validate(row(path))
```

Run `.venv/bin/python -m pytest tests/strategy/test_authored_wire.py -q`: FAIL (`ValidationError` on the authored row: the pattern is catalogue only).

In `trader/messaging/ai_deployment_wire.py` add `from trader.research.strategy_key import AUTHORED_PATH` and, under `_STRICT`:

```python
# A catalogue file or an authored file (SP3 Plan 3 ruling 1, decision D4). The trader validates its own reply with
# this model, so a row this pattern refuses would fail the reply for every deployment.
ACTIVE_STRATEGY_PATH = rf"^(?:{_PATH.pattern[1:-1]}|{AUTHORED_PATH.pattern[1:-1]})$"
```

and in `ActiveAiDeployment`:

```python
    strategy_path: Annotated[str, Field(pattern=ACTIVE_STRATEGY_PATH)]
```

- [ ] **Step 8: Run the tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/automation/test_authored_keys_in_sp2c.py tests/strategy/test_authored_wire.py tests/test_strategy_ai_deployments.py tests/automation/test_backtest_judgments.py tests/automation/test_ai_judgment_port.py tests/automation/test_deployment_renewal.py tests/automation/test_ai_deployments.py tests/automation/test_ai_deployment_registration.py tests/research/test_evaluation_request_and_case.py tests/research/test_renewal_case.py -q --timeout=60`
Expected: PASS.

- [ ] **Step 9: Commit**

```bash
git add trader/research/strategy_key.py trader/research/evaluation_request.py trader/research/evaluation_case.py \
  trader/automation/backtest_judgments.py trader/automation/ai_judgment_port.py \
  trader/automation/deployment_renewal.py trader/automation/ai_deployments.py \
  trader/messaging/ai_deployment_wire.py tests/automation/test_authored_keys_in_sp2c.py \
  tests/strategy/test_authored_wire.py
git commit -m "feat: let sp2c claims, cases and deployments carry an authored key

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---
### Task 4: Tables 130-131, the file store and the record states

**Files:**
- Create: `trader/automation/authored_schema.py`, `trader/automation/authored_refusals.py`, `trader/automation/authored_files.py`, `trader/automation/authored_strategies.py` (core only; Tasks 5-8 add to it)
- Modify: `trader/data/schema_migrations.py` (docstring), `trader/trading/command_stack.py` (migrations), `docker-compose.yml`, `Dockerfile`, `docker.sh`
- Test: `tests/automation/authored_fixtures.py` (new helpers), `tests/automation/test_authored_records.py`, `tests/automation/test_authored_files.py`, `tests/test_compose_authored_store.py`

**Interfaces:**
- Consumes: `SchemaMigrator.apply`, `utc` (`trader/automation/evaluation_claims.py`), `canonical_json_bytes`, Plan 1 `authored_key`.
- Produces:
  - `authored_schema`: `AUTHORED_STRATEGIES_MIGRATION_VERSION = 130`, `ISOLATION_PROOFS_MIGRATION_VERSION = 131`, `apply_authored_migrations(migrator) -> list[int]`.
  - `authored_refusals`: the code constants and `AuthoredRefused(code, detail, *, retryable=False)` with `.reply(**extra) -> dict`.
  - `authored_files`: `STORE_DIR_ENV = "MMR_AUTHORED_STORE_DIR"`, `DEFAULT_STORE_DIR`, `default_store_dir() -> Path`, `AuthoredStoreError`, `AuthoredFileStore(root)` with `.write(digest: str, source: bytes, meta: Mapping) -> None` and `.source_path(digest) -> Path`.
  - `authored_strategies`: `AuthoredState` (StrEnum), `STORED` … `FAILED`, `COUNTED_STATES`, `TRANSITIONS`, the reason constants, `AuthoredRecord` (`.key`, `.to_json()`, `.file_metadata()`), `new_record(...)`, `record_in_tx`, `records_in_tx`, `counted_in_tx`, `judged_count_in_tx`, `insert_in_tx`, `move_in_tx(conn, record, target, *, event, reason, now, request_id=None, detail=None, **changes)`, `note_in_tx(conn, record, *, event, now, request_id=None, detail=None, image=None, **changes)`.

- [ ] **Step 1: Write the shared test helpers** (`tests/automation/authored_fixtures.py`)

```python
"""Shared helpers for SP3 Plan 3 tests: an authored file, its tunables and journals with the tables."""
from __future__ import annotations

import hashlib
from pathlib import Path

from trader.automation.authored_schema import apply_authored_migrations
from trader.automation.backtest_judge_schema import apply_backtest_judge_migrations
from trader.data.duckdb_store import DuckDBConnection
from trader.data.schema_migrations import SchemaMigrator

# Written against spec 7.2: allowed imports only, no name or attribute starting with "_", exactly one class.
VALID_SOURCE = '''\
import pandas as pd

from trader.authored.api import AuthoredStrategy, Decision


class RangeBreak(AuthoredStrategy):
    LOOKBACK_BARS = 40
    RANGE_BARS = 15
    MIN_PROBABILITY = 0.6

    def on_bar(self, history, params):
        bars = int(params["RANGE_BARS"])
        if len(history) <= bars:
            return None
        ceiling = history["high"].iloc[-bars - 1:-1].max()
        if history["close"].iloc[-1] > ceiling:
            return Decision(action="BUY", probability=float(params["MIN_PROBABILITY"]))
        return None
'''
def tunables_for(range_bars: int = 15) -> dict:
    """The submit tunables of ``source(range_bars)``: each default is the class value (ruling 5)."""
    return {"RANGE_BARS": {"low": 5, "high": 30, "default": range_bars},
            "MIN_PROBABILITY": {"low": 0.5, "high": 0.9, "default": 0.6}}


TUNABLES = tunables_for()
IMAGE = "a1b2c3d-20261011T220000Z"             # Plan 2 build ids (<git sha>-<UTC time>)
OTHER_IMAGE = "e4f5a6b-20261012T090000Z"


def source(range_bars: int = 15) -> str:
    """A distinct authored file per value; 5-30 stays inside TUNABLES."""
    return VALID_SOURCE.replace("RANGE_BARS = 15", f"RANGE_BARS = {range_bars}")


def digest_of(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def request_id(n: int) -> str:
    return "sha256:" + f"{n:064x}"


def authored_journal(tmp_path: Path) -> DuckDBConnection:
    """A journal with the SP2c claim and judgment tables and this plan's tables 130-131."""
    db = DuckDBConnection.get_instance(str(tmp_path / "journal.duckdb"))
    migrator = SchemaMigrator(db)
    apply_backtest_judge_migrations(migrator)
    apply_authored_migrations(migrator)
    return db
```

- [ ] **Step 2: Write the failing tests** (`tests/automation/test_authored_records.py`)

```python
"""SP3 Plan 3 Task 4: tables 130-131 and the record states (spec 7.3; ruling 10)."""
from __future__ import annotations

import duckdb
import pytest

from tests.automation.authored_fixtures import TUNABLES, authored_journal, digest_of, request_id, source
from tests.automation.backtest_judge_fixtures import NOW
from trader.automation.authored_refusals import AuthoredRefused
from trader.automation.authored_schema import apply_authored_migrations
from trader.automation.authored_strategies import (
    COUNTED_STATES, DEPLOYED, EVALUATING, EXPIRED, FAILED, REJECTED, RETIRED, SHADOW, STORED, TRANSITIONS,
    AuthoredState, counted_in_tx, insert_in_tx, move_in_tx, new_record, record_in_tx, records_in_tx,
)
from trader.data.duckdb_store import DuckDBConnection
from trader.data.schema_migrations import SchemaMigrator

DIGEST = digest_of(source())


def fresh(digest: str = DIGEST):
    return new_record(digest=digest, class_name="RangeBreak", bar_size="5 mins", tunables=TUNABLES,
                      note="range break", author_model="openrouter/author-1", now=NOW)


def test_migrations_130_and_131_apply_once(tmp_path):
    db = DuckDBConnection.get_instance(str(tmp_path / "plain.duckdb"))
    migrator = SchemaMigrator(db)
    assert apply_authored_migrations(migrator) == [130, 131]
    assert apply_authored_migrations(migrator) == []
    tables = {row[0] for row in db.execute("SELECT table_name FROM information_schema.tables", fetch="all")}
    assert {"authored_strategies", "authored_isolation_proofs"} <= tables


def test_the_states_are_the_spec_table():
    assert set(AuthoredState) == {STORED, EVALUATING, SHADOW, DEPLOYED, REJECTED, RETIRED, EXPIRED, FAILED}
    assert COUNTED_STATES == {STORED, EVALUATING, SHADOW, DEPLOYED}
    assert set(TRANSITIONS) == set(COUNTED_STATES)                    # terminal states have no way out
    assert TRANSITIONS[STORED] == {EVALUATING}
    assert TRANSITIONS[EVALUATING] == {REJECTED, SHADOW, DEPLOYED, STORED, FAILED}
    assert TRANSITIONS[SHADOW] == {RETIRED, EXPIRED, DEPLOYED}         # ruling 10
    assert TRANSITIONS[DEPLOYED] == {RETIRED, EXPIRED}


def test_a_record_round_trips_and_moves_along_the_table(tmp_path):
    db = authored_journal(tmp_path)
    record = fresh()
    db.transaction(lambda conn: insert_in_tx(conn, record))
    assert db.transaction(lambda conn: record_in_tx(conn, DIGEST)) == record
    moved = db.transaction(lambda conn: move_in_tx(
        conn, record, EVALUATING, event="CLAIMED", reason=None, now=NOW, request_id=request_id(1),
        evaluation_request_id=request_id(1)))
    assert (moved.state, moved.evaluation_request_id) == (EVALUATING, request_id(1))
    assert [entry["to"] for entry in moved.history] == ["STORED", "EVALUATING"]
    assert db.transaction(lambda conn: record_in_tx(conn, DIGEST)) == moved
    assert db.transaction(counted_in_tx) == 1
    assert moved.key == f"authored:{DIGEST}:RangeBreak"


def test_an_illegal_move_is_refused_and_writes_nothing(tmp_path):
    db = authored_journal(tmp_path)
    record = fresh()
    db.transaction(lambda conn: insert_in_tx(conn, record))
    with pytest.raises(AuthoredRefused) as refused:
        db.transaction(lambda conn: move_in_tx(conn, record, DEPLOYED, event="X", reason=None, now=NOW))
    assert refused.value.code == "AUTHORED_STATE_CONFLICT"
    assert db.transaction(lambda conn: record_in_tx(conn, DIGEST)).state == STORED


def test_a_terminal_record_never_moves_and_is_not_counted(tmp_path):
    db = authored_journal(tmp_path)
    record = fresh()
    db.transaction(lambda conn: insert_in_tx(conn, record))
    evaluating = db.transaction(lambda conn: move_in_tx(conn, record, EVALUATING, event="CLAIMED", reason=None,
                                                        now=NOW))
    rejected = db.transaction(lambda conn: move_in_tx(conn, evaluating, REJECTED, event="JUDGED",
                                                      reason="JEV_REJECT", now=NOW))
    with pytest.raises(AuthoredRefused):
        db.transaction(lambda conn: move_in_tx(conn, rejected, STORED, event="X", reason=None, now=NOW))
    assert db.transaction(counted_in_tx) == 0
    assert [r.digest for r in db.transaction(records_in_tx)] == [DIGEST]


def test_the_table_refuses_an_unknown_state(tmp_path):
    db = authored_journal(tmp_path)
    with pytest.raises(duckdb.ConstraintException):
        db.execute("INSERT INTO authored_strategies (digest, class_name, bar_size, tunables_json, note, author_model, "
                   "state, attempts, created_at, state_changed_at, history_json) "
                   "VALUES ('d', 'C', '5 mins', '{}', '', 'm', 'LIVE', 0, now(), now(), '[]')")
```

`tests/automation/test_authored_files.py`:

```python
"""SP3 Plan 3 Task 4: the trader's writer of the content-addressed store (spec 7.3, 8; review focus 3)."""
from __future__ import annotations

import json
import stat
from pathlib import Path

import pytest

from tests.automation.authored_fixtures import VALID_SOURCE, digest_of
from trader.automation.authored_files import (
    DEFAULT_STORE_DIR, STORE_DIR_ENV, AuthoredFileStore, AuthoredStoreError, default_store_dir,
)

DATA = VALID_SOURCE.encode("utf-8")
DIGEST = digest_of(VALID_SOURCE)
HEX = DIGEST.removeprefix("sha256:")


def mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def test_write_puts_the_source_and_then_its_metadata(tmp_path):
    store = AuthoredFileStore(tmp_path / "store")
    store.write(DIGEST, DATA, {"class_name": "RangeBreak"})
    assert store.source_path(DIGEST) == tmp_path / "store" / f"{HEX}.py"
    assert (tmp_path / "store" / f"{HEX}.py").read_bytes() == DATA
    assert json.loads((tmp_path / "store" / f"{HEX}.json").read_text()) == {"class_name": "RangeBreak"}
    assert mode(tmp_path / "store" / f"{HEX}.py") == mode(tmp_path / "store" / f"{HEX}.json") == 0o644
    assert not list((tmp_path / "store").glob(".*.tmp"))


def test_bytes_that_do_not_match_their_digest_are_refused(tmp_path):
    with pytest.raises(AuthoredStoreError):
        AuthoredFileStore(tmp_path / "store").write(DIGEST, DATA + b"\n", {})
    assert not (tmp_path / "store" / f"{HEX}.py").exists()


def test_a_file_with_other_bytes_under_the_digest_is_refused(tmp_path):                 # review focus 3
    (tmp_path / "store").mkdir()
    (tmp_path / "store" / f"{HEX}.py").write_bytes(b"print('swapped')\n")
    with pytest.raises(AuthoredStoreError, match="other bytes"):
        AuthoredFileStore(tmp_path / "store").write(DIGEST, DATA, {})
    assert (tmp_path / "store" / f"{HEX}.py").read_bytes() == b"print('swapped')\n"


def test_writing_the_same_digest_again_keeps_the_file_and_replaces_the_metadata(tmp_path):
    store = AuthoredFileStore(tmp_path / "store")
    store.write(DIGEST, DATA, {"note": "first"})
    store.write(DIGEST, DATA, {"note": "second"})
    assert json.loads((tmp_path / "store" / f"{HEX}.json").read_text()) == {"note": "second"}


def test_a_crash_before_the_metadata_leaves_only_the_source(tmp_path, monkeypatch):     # spec 8
    store = AuthoredFileStore(tmp_path / "store")
    real_replace = AuthoredFileStore._replace

    def full_disk_on_metadata(self, path, data):
        if path.suffix == ".json":
            raise OSError("no space left on device")
        return real_replace(self, path, data)
    monkeypatch.setattr(AuthoredFileStore, "_replace", full_disk_on_metadata)
    with pytest.raises(OSError):
        store.write(DIGEST, DATA, {})
    assert (tmp_path / "store" / f"{HEX}.py").exists() and not (tmp_path / "store" / f"{HEX}.json").exists()
    monkeypatch.undo()
    store.write(DIGEST, DATA, {})
    assert (tmp_path / "store" / f"{HEX}.json").exists()


def test_a_symlinked_store_directory_is_refused(tmp_path):
    (tmp_path / "real").mkdir()
    (tmp_path / "link").symlink_to(tmp_path / "real")
    with pytest.raises(AuthoredStoreError, match="not a real directory"):
        AuthoredFileStore(tmp_path / "link").write(DIGEST, DATA, {})


def test_the_default_directory_follows_the_environment(tmp_path, monkeypatch):
    monkeypatch.setenv(STORE_DIR_ENV, str(tmp_path / "elsewhere"))
    assert default_store_dir() == tmp_path / "elsewhere"
    monkeypatch.delenv(STORE_DIR_ENV)
    assert default_store_dir() == Path(DEFAULT_STORE_DIR).expanduser()
```

`tests/test_compose_authored_store.py`:

```python
"""SP3 Plan 3 Task 4: the store volume is read-write in the trader only (spec 7.3; ruling 20)."""
from __future__ import annotations

import pytest

from tests.compose_rpc_helpers import ROOT, load_compose, volumes

TARGET = "/store"                     # Plan 2 ruling 22


def test_the_trader_mounts_the_store_read_write():
    compose = load_compose()
    assert "mmr_authored_strategies" in compose["volumes"]
    mounts = [v for v in volumes(compose["services"]["trader"]) if v["source"] == "mmr_authored_strategies"]
    assert mounts == [{"type": "volume", "source": "mmr_authored_strategies", "target": TARGET, "read_only": False}]
    assert compose["services"]["trader"]["environment"]["MMR_AUTHORED_STORE_DIR"] == TARGET


@pytest.mark.parametrize("name", ["ai", "research", "strategy", "dashboard", "data", "scheduler", "cli"])
def test_no_other_service_of_this_plan_mounts_the_store(name):
    service = load_compose()["services"][name]
    assert not [v for v in volumes(service) if v["source"] == "mmr_authored_strategies"]


def test_the_image_creates_the_store_directory_owned_by_trader():
    assert "mkdir -p /store && chown trader:trader /store" in (ROOT / "Dockerfile").read_text()


def test_the_key_check_removes_its_own_store_volume():
    assert 'volume rm "${KEYCHECK_PROJECT}_mmr_authored_strategies"' in (ROOT / "docker.sh").read_text()
```

- [ ] **Step 3: Run the tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/automation/test_authored_records.py tests/automation/test_authored_files.py tests/test_compose_authored_store.py -q --timeout=60`
Expected: FAIL (`ModuleNotFoundError: trader.automation.authored_schema`).

- [ ] **Step 4: Write the refusal codes** (`trader/automation/authored_refusals.py`)

```python
"""Refusal codes of the authored-strategy methods (SP3 spec 7.3) and their exception.

A refusal is a reply body, never an RPC error. Kept apart from the store so the submission checks and
the store can both raise it without importing each other.
"""
from __future__ import annotations

AUTHORED_STRATEGIES_DISABLED = "AUTHORED_STRATEGIES_DISABLED"
AUTHORED_CAP_FULL = "AUTHORED_CAP_FULL"
AUTHORED_STATIC_REJECTED = "AUTHORED_STATIC_REJECTED"
AUTHORED_BAD_TUNABLES = "AUTHORED_BAD_TUNABLES"
AUTHORED_BAD_BAR_SIZE = "AUTHORED_BAD_BAR_SIZE"
AUTHORED_ISOLATION_UNPROVEN = "AUTHORED_ISOLATION_UNPROVEN"
AUTHORED_STATE_CONFLICT = "AUTHORED_STATE_CONFLICT"
AUTHORED_STRATEGY_UNKNOWN = "AUTHORED_STRATEGY_UNKNOWN"


class AuthoredRefused(Exception):
    def __init__(self, code: str, detail: str, *, retryable: bool = False):
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail
        self.retryable = retryable

    def reply(self, **extra) -> dict:
        return {"status": "REFUSED", "code": self.code, "detail": self.detail, "retryable": self.retryable,
                "record": None, **extra}
```

- [ ] **Step 5: Write the tables** (`trader/automation/authored_schema.py`)

```python
"""Journal tables of SP3 authored strategies (migrations 130 and 131; 132 is SP3 Plan 5's, 133-134 stay free)."""
from __future__ import annotations

from typing import Any

from trader.automation.authored_strategies import AuthoredState

AUTHORED_STRATEGIES_MIGRATION_VERSION = 130
ISOLATION_PROOFS_MIGRATION_VERSION = 131
_STATES = ", ".join(f"'{state.value}'" for state in AuthoredState)

_AUTHORED = f"""CREATE TABLE IF NOT EXISTS authored_strategies (
    digest VARCHAR PRIMARY KEY, class_name VARCHAR NOT NULL, bar_size VARCHAR NOT NULL,
    tunables_json VARCHAR NOT NULL, note VARCHAR NOT NULL, author_model VARCHAR NOT NULL,
    state VARCHAR NOT NULL CHECK (state IN ({_STATES})),
    reason VARCHAR, attempts INTEGER NOT NULL CHECK (attempts >= 0),
    evaluation_request_id VARCHAR, sandbox_image_digest VARCHAR, sandbox_image_seen_at TIMESTAMPTZ,
    verdict VARCHAR CHECK (verdict IS NULL OR verdict IN ('DEPLOY','SHADOW','REJECT','NO_VERDICT')),
    judgment_id VARCHAR, deployment_version VARCHAR,
    created_at TIMESTAMPTZ NOT NULL, state_changed_at TIMESTAMPTZ NOT NULL, history_json VARCHAR NOT NULL)"""

_PROOFS = """CREATE TABLE IF NOT EXISTS authored_isolation_proofs (
    proof_id VARCHAR PRIMARY KEY, image_digest VARCHAR NOT NULL, passed BOOLEAN NOT NULL,
    checks_json VARCHAR NOT NULL, ran_at TIMESTAMPTZ NOT NULL, recorded_at TIMESTAMPTZ NOT NULL,
    principal VARCHAR NOT NULL)"""


def apply_authored_migrations(migrator: Any) -> list[int]:
    applied = []
    if migrator.apply(AUTHORED_STRATEGIES_MIGRATION_VERSION, "sp3_authored_strategies", (_AUTHORED,)):
        applied.append(AUTHORED_STRATEGIES_MIGRATION_VERSION)
    if migrator.apply(ISOLATION_PROOFS_MIGRATION_VERSION, "sp3_authored_isolation_proofs", (_PROOFS,)):
        applied.append(ISOLATION_PROOFS_MIGRATION_VERSION)
    return applied
```

- [ ] **Step 6: Write the file store** (`trader/automation/authored_files.py`)

```python
"""The trader's writer of the content-addressed store (SP3 spec 7.3, 8).

``<hex>.py`` holds the authored file's exact bytes, ``<hex>.json`` its metadata. Each goes to a fresh
temp file, is flushed to disk and renamed into place, ``.py`` before ``.json``; the caller writes the
journal record only after both. A crash leaves at most an orphan file, never a record without its file.
The sandbox mounts the same volume read-only, so files are 0644, and a ``.py`` is never rewritten with
other bytes: a mismatch is an error, not a repair.
"""
from __future__ import annotations

import hashlib
import os
import re
import secrets
import stat
from pathlib import Path
from typing import Mapping

from trader.research.canonical import canonical_json_bytes

STORE_DIR_ENV = "MMR_AUTHORED_STORE_DIR"
DEFAULT_STORE_DIR = "~/.local/share/mmr/authored_strategies"
FILE_MODE = 0o644
DIR_MODE = 0o755
_DIGEST = re.compile(r"^sha256:([0-9a-f]{64})$")


class AuthoredStoreError(Exception):
    """The store directory or a stored file is unusable; the message names the path, never file bytes."""


def default_store_dir() -> Path:
    return Path(os.environ.get(STORE_DIR_ENV) or DEFAULT_STORE_DIR).expanduser()


def _hex(digest: str) -> str:
    match = _DIGEST.fullmatch(digest) if isinstance(digest, str) else None
    if match is None:
        raise AuthoredStoreError(f"{digest!r} is not sha256:<64 hex>")
    return match.group(1)


class AuthoredFileStore:
    def __init__(self, root: Path):
        self._root = Path(root)

    def source_path(self, digest: str) -> Path:
        return self._root / f"{_hex(digest)}.py"

    def write(self, digest: str, source: bytes, meta: Mapping) -> None:
        hex_digest = _hex(digest)
        if hashlib.sha256(source).hexdigest() != hex_digest:
            raise AuthoredStoreError("the source bytes do not match their digest")
        self._prepare_root()
        source_path = self._root / f"{hex_digest}.py"
        if not self._holds(source_path, source):
            self._replace(source_path, source)
        self._replace(self._root / f"{hex_digest}.json", canonical_json_bytes(dict(meta)))

    def _prepare_root(self) -> None:
        self._root.mkdir(parents=True, exist_ok=True, mode=DIR_MODE)
        info = os.lstat(self._root)
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise AuthoredStoreError(f"{self._root} is not a real directory")

    @staticmethod
    def _holds(path: Path, data: bytes) -> bool:
        """True when ``path`` already holds exactly ``data`` (an orphan of an earlier crash)."""
        try:
            info = os.lstat(path)
        except FileNotFoundError:
            return False
        if not stat.S_ISREG(info.st_mode):
            raise AuthoredStoreError(f"{path} is not a regular file")
        if path.read_bytes() != data:
            raise AuthoredStoreError(f"{path} holds other bytes than its digest names")
        return True

    def _replace(self, path: Path, data: bytes) -> None:
        temp = path.with_name(f".{path.name}.{secrets.token_hex(8)}.tmp")
        descriptor = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, FILE_MODE)
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(temp, FILE_MODE)
            os.replace(temp, path)
        except BaseException:
            temp.unlink(missing_ok=True)
            raise
        self._sync_root()

    def _sync_root(self) -> None:
        descriptor = os.open(self._root, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
```

- [ ] **Step 7: Write the record core** (`trader/automation/authored_strategies.py`)

```python
"""Authored-strategy records, their states and the cap (SP3 spec 7.3, 8).

One journal row per digest. ``COUNTED_STATES`` count toward ``ai_paper.authored_strategies.max_active``;
every other state is terminal. A row moves only along ``TRANSITIONS`` and each move appends to its
history. Functions ending ``_in_tx`` run inside the caller's ``DuckDBConnection.transaction`` and never
call the store's own ``execute`` or ``transaction``.
"""
from __future__ import annotations

import datetime as dt
import json
from dataclasses import dataclass, replace
from enum import StrEnum
from types import MappingProxyType
from typing import Any, Mapping, Optional

from trader.authored.keys import authored_key
from trader.automation.authored_refusals import AUTHORED_STATE_CONFLICT, AuthoredRefused
from trader.automation.evaluation_claims import utc
from trader.research.canonical import canonical_json_bytes

FILE_SCHEMA = "mmr.authored-strategy.v1"


class AuthoredState(StrEnum):
    STORED = "STORED"
    EVALUATING = "EVALUATING"
    SHADOW = "SHADOW"
    DEPLOYED = "DEPLOYED"
    REJECTED = "REJECTED"
    RETIRED = "RETIRED"
    EXPIRED = "EXPIRED"
    FAILED = "FAILED"


STORED, EVALUATING, SHADOW, DEPLOYED = (
    AuthoredState.STORED, AuthoredState.EVALUATING, AuthoredState.SHADOW, AuthoredState.DEPLOYED)
REJECTED, RETIRED, EXPIRED, FAILED = (
    AuthoredState.REJECTED, AuthoredState.RETIRED, AuthoredState.EXPIRED, AuthoredState.FAILED)
COUNTED_STATES = frozenset({STORED, EVALUATING, SHADOW, DEPLOYED})
# Spec 7.3, plus SHADOW -> DEPLOYED for a DEPLOY verdict that waited for an isolation proof (spec 8, ruling 10).
TRANSITIONS: Mapping[AuthoredState, frozenset[AuthoredState]] = MappingProxyType({
    STORED: frozenset({EVALUATING}),
    EVALUATING: frozenset({REJECTED, SHADOW, DEPLOYED, STORED, FAILED}),
    SHADOW: frozenset({RETIRED, EXPIRED, DEPLOYED}),
    DEPLOYED: frozenset({RETIRED, EXPIRED}),
})
UNAVAILABLE_ATTEMPTS = 3

# Reasons a record carries (spec 7.3; rulings 9 and 11).
SANDBOX_ERROR = "SANDBOX_ERROR"
SANDBOX_UNAVAILABLE = "SANDBOX_UNAVAILABLE"
JEV_REJECT = "JEV_REJECT"
JEV_SHADOW = "JEV_SHADOW"
JEV_NO_VERDICT = "JEV_NO_VERDICT"
AWAITING_REGISTRATION = "AWAITING_REGISTRATION"
OWNER_RETIRED = "OWNER_RETIRED"
SHADOW_EXPIRED = "SHADOW_EXPIRED"
DEPLOY_EXPIRED = "DEPLOY_EXPIRED"
RENEWAL_ENDED = "RENEWAL_ENDED"

_COLUMNS = ("digest", "class_name", "bar_size", "tunables_json", "note", "author_model", "state", "reason",
            "attempts", "evaluation_request_id", "sandbox_image_digest", "sandbox_image_seen_at", "verdict",
            "judgment_id", "deployment_version", "created_at", "state_changed_at", "history_json")
_SELECT = f"SELECT {', '.join(_COLUMNS)} FROM authored_strategies"


@dataclass(frozen=True)
class AuthoredRecord:
    digest: str
    class_name: str
    bar_size: str
    tunables: Mapping[str, Mapping[str, float]]
    note: str
    author_model: str
    state: AuthoredState
    reason: Optional[str]
    attempts: int
    evaluation_request_id: Optional[str]
    sandbox_image_digest: Optional[str]
    sandbox_image_seen_at: Optional[dt.datetime]
    verdict: Optional[str]
    judgment_id: Optional[str]
    deployment_version: Optional[str]
    created_at: dt.datetime
    state_changed_at: dt.datetime
    history: tuple[dict, ...]

    @property
    def key(self) -> str:
        return authored_key(self.digest, self.class_name)

    def _tunables_json(self) -> dict:
        return {name: dict(bounds) for name, bounds in self.tunables.items()}

    def to_json(self) -> dict:
        return {"digest": self.digest, "key": self.key, "class_name": self.class_name, "bar_size": self.bar_size,
                "tunables": self._tunables_json(), "note": self.note, "author_model": self.author_model,
                "state": self.state.value, "reason": self.reason, "attempts": self.attempts,
                "evaluation_request_id": self.evaluation_request_id,
                "sandbox_image_digest": self.sandbox_image_digest, "verdict": self.verdict,
                "judgment_id": self.judgment_id, "deployment_version": self.deployment_version,
                "created_at": self.created_at.isoformat(), "state_changed_at": self.state_changed_at.isoformat(),
                "history": [dict(entry) for entry in self.history]}

    def file_metadata(self) -> dict:
        """The ``<hex>.json`` beside the file (spec 7.3). States live in the journal only."""
        return {"schema": FILE_SCHEMA, "digest": self.digest, "class_name": self.class_name,
                "tunables": self._tunables_json(), "bar_size": self.bar_size, "note": self.note,
                "author_model": self.author_model, "created_at": self.created_at.isoformat()}


def _entry(now: dt.datetime, *, event: str, source: Optional[AuthoredState], target: AuthoredState,
           reason: Optional[str], request_id: Optional[str] = None, detail: Optional[str] = None,
           image: Optional[str] = None) -> dict:
    return {"at": now.isoformat(), "event": event, "from": None if source is None else source.value,
            "to": target.value, "reason": reason, "request_id": request_id, "detail": detail, "image": image}


def new_record(*, digest: str, class_name: str, bar_size: str, tunables: Mapping[str, Mapping[str, float]],
               note: str, author_model: str, now: dt.datetime) -> AuthoredRecord:
    now = utc(now)
    entry = _entry(now, event="SUBMITTED", source=None, target=STORED, reason=None)
    return AuthoredRecord(digest, class_name, bar_size, {n: dict(b) for n, b in tunables.items()}, note,
                          author_model, STORED, None, 0, None, None, None, None, None, None, now, now, (entry,))


def _text(value: Any) -> str:
    return canonical_json_bytes(value).decode("utf-8")


def _values(record: AuthoredRecord) -> list:
    return [record.digest, record.class_name, record.bar_size, _text(record._tunables_json()), record.note,
            record.author_model, record.state.value, record.reason, record.attempts, record.evaluation_request_id,
            record.sandbox_image_digest, record.sandbox_image_seen_at, record.verdict, record.judgment_id,
            record.deployment_version, record.created_at, record.state_changed_at, _text(list(record.history))]


def _record(row: tuple) -> AuthoredRecord:
    seen_at = None if row[11] is None else utc(row[11])
    return AuthoredRecord(row[0], row[1], row[2], json.loads(row[3]), row[4], row[5], AuthoredState(row[6]),
                          row[7], row[8], row[9], row[10], seen_at, row[12], row[13], row[14], utc(row[15]),
                          utc(row[16]), tuple(json.loads(row[17])))


def record_in_tx(conn: Any, digest: str) -> Optional[AuthoredRecord]:
    row = conn.execute(f"{_SELECT} WHERE digest = ?", [digest]).fetchone()
    return None if row is None else _record(row)


def records_in_tx(conn: Any) -> tuple[AuthoredRecord, ...]:
    return tuple(_record(row) for row in conn.execute(f"{_SELECT} ORDER BY created_at, digest").fetchall())


def counted_in_tx(conn: Any) -> int:
    states = sorted(state.value for state in COUNTED_STATES)
    marks = ", ".join("?" for _ in states)
    return conn.execute(f"SELECT COUNT(*) FROM authored_strategies WHERE state IN ({marks})", states).fetchone()[0]


def judged_count_in_tx(conn: Any) -> int:
    """Spec 7.7 and ruling 19: authored strategies that reached a judgment, whatever the verdict."""
    return conn.execute("SELECT COUNT(*) FROM authored_strategies WHERE verdict IS NOT NULL").fetchone()[0]


def insert_in_tx(conn: Any, record: AuthoredRecord) -> None:
    marks = ", ".join("?" for _ in _COLUMNS)
    conn.execute(f"INSERT INTO authored_strategies ({', '.join(_COLUMNS)}) VALUES ({marks})", _values(record))


def _update_in_tx(conn: Any, record: AuthoredRecord) -> None:
    assignments = ", ".join(f"{name} = ?" for name in _COLUMNS[1:])
    conn.execute(f"UPDATE authored_strategies SET {assignments} WHERE digest = ?",
                 _values(record)[1:] + [record.digest])


def move_in_tx(conn: Any, record: AuthoredRecord, target: AuthoredState, *, event: str, reason: Optional[str],
               now: dt.datetime, request_id: Optional[str] = None, detail: Optional[str] = None,
               **changes: Any) -> AuthoredRecord:
    if target not in TRANSITIONS.get(record.state, frozenset()):
        raise AuthoredRefused(AUTHORED_STATE_CONFLICT, f"{record.state.value} cannot move to {target.value}")
    now = utc(now)
    entry = _entry(now, event=event, source=record.state, target=target, reason=reason, request_id=request_id,
                   detail=detail)
    moved = replace(record, state=target, reason=reason, state_changed_at=now, history=record.history + (entry,),
                    **changes)
    _update_in_tx(conn, moved)
    return moved


def note_in_tx(conn: Any, record: AuthoredRecord, *, event: str, now: dt.datetime,
               request_id: Optional[str] = None, detail: Optional[str] = None, image: Optional[str] = None,
               **changes: Any) -> AuthoredRecord:
    """A history entry without a state change (research's STARTED report)."""
    entry = _entry(utc(now), event=event, source=record.state, target=record.state, reason=record.reason,
                   request_id=request_id, detail=detail, image=image)
    noted = replace(record, history=record.history + (entry,), **changes)
    _update_in_tx(conn, noted)
    return noted
```

- [ ] **Step 8: Register the migrations and document the numbers**

`trader/trading/command_stack.py`, right after `apply_backtest_judge_migrations(migrator)  # 110, 111 ...`:

```python
    from trader.automation.authored_schema import apply_authored_migrations

    apply_authored_migrations(migrator)               # 130, 131 (SP3 Plan 3)
```

`trader/data/schema_migrations.py`, in the module docstring after the SP2c Plan 5 line, add:

```
SP3 Plan 3 uses **130** (``authored_strategies``) and **131** (``authored_isolation_proofs``); SP3 Plan 5 uses **132** (``ai_costs.role`` gains ``author``); 133–134 stay free.
```

- [ ] **Step 9: Mount the store in the trader**

`docker-compose.yml`, in the `trader` service after `- mmr_db_data:/home/trader/.local/share/mmr/data`, add:

```yaml
      # SP3 store of authored files (Plan 3 ruling 20, Plan 2 ruling 22): the trader writes it; only the
      # sandbox also mounts it, read-only, at the same path.
      - mmr_authored_strategies:/store
```

and in its `environment:` block, after `TYPED_BIND_ADDRESS`, add:

```yaml
      MMR_AUTHORED_STORE_DIR: /store
```

At the end of the top-level `volumes:` block add `  mmr_authored_strategies:` unless Plan 2 already added it (one entry only).

`Dockerfile`: unless Plan 2 already added it, add after the `RUN mkdir -p /home/trader/.local/share/mmr/data` block:

```dockerfile
RUN mkdir -p /store && chown trader:trader /store && chmod 0755 /store
```

so a fresh named volume starts owned by `trader` (Docker copies the image directory's owner into an empty volume).

`docker.sh`, in `key_check()`, after the `mmr_research_data` removal line, add:

```bash
    $RUNTIME volume rm "${KEYCHECK_PROJECT}_mmr_authored_strategies" >/dev/null 2>&1 || true
```

- [ ] **Step 10: Run the tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/automation/test_authored_records.py tests/automation/test_authored_files.py tests/test_compose_authored_store.py tests/test_compose_rpc_keys.py tests/test_docker_helper.py -q --timeout=60`
Expected: PASS.

- [ ] **Step 11: Commit**

```bash
git add trader/automation/authored_schema.py trader/automation/authored_refusals.py \
  trader/automation/authored_files.py trader/automation/authored_strategies.py trader/data/schema_migrations.py \
  trader/trading/command_stack.py docker-compose.yml Dockerfile docker.sh tests/automation/authored_fixtures.py \
  tests/automation/test_authored_records.py tests/automation/test_authored_files.py tests/test_compose_authored_store.py
git commit -m "feat: add the authored-strategy tables, file store and record states

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---
### Task 5: `submit_authored_strategy`: checks, the store write and the cap

**Files:**
- Create: `trader/automation/authored_wire.py`, `trader/automation/authored_submission.py`
- Modify: `trader/automation/authored_strategies.py` (add `AuthoredStrategyStore`), `tests/automation/authored_fixtures.py`
- Test: `tests/automation/test_authored_submission.py`

**Interfaces:**
- Consumes: Task 4 core; Plan 1 `require_clean`, `AuthoredStaticRejected`, `source_digest`; `AuthoredStrategiesConfig`; `TUNABLE_NAME`, `MAX_PARAMS` (`trader/research/request_rules.py`); `bar_size_key` (`trader/automation/strategy_binding.py`); `DIGEST`, `JEV_MODEL`, `parse_aware` (`trader/automation/backtest_judge_wire.py`).
- Produces:
  - `authored_wire`: `TunableRange`, `SubmitAuthoredStrategyRequest`, `GetAuthoredStrategiesRequest`, `ReportAuthoredEvaluationRequest`, `RetireAuthoredStrategyRequest`, `IsolationCheck`, `RecordIsolationProofRequest` (`.ran_at_utc()`), `GetIsolationProofRequest` (shapes in Cross-plan additions).
  - `authored_submission`: `NOT_TUNABLE`, `CheckedSubmission(class_name, bar_size, tunables)`, `check_submission(request, config) -> CheckedSubmission`, `class_constants(source, class_name) -> dict[str, float]`.
  - `AuthoredStrategyStore(db, *, config, files, now, proofs=None, calendar=None, shadow_sessions=20, lines=None, withdraw=None)` with `.config`, `.get(digest) -> AuthoredRecord | None`, `.submit(request) -> dict`, `.overview() -> dict`. (`proofs` is read in Task 8; `calendar`, `shadow_sessions`, `lines` and `withdraw` in Task 6.)
  - Fixtures: `submit_request(text=None, **changes)`, `AuthoredWorld(db, clock, store, tmp_path)`, `authored_world(tmp_path, *, lines=None, calendar=None, proofs=None, withdraw=None, **config)`, `with_config(world, **config) -> AuthoredStrategyStore`, `stored(world, range_bars=15) -> str`, `stored_files(world) -> list[Path]`.

- [ ] **Step 1: Add the fixtures** (append to `tests/automation/authored_fixtures.py`)

Add these imports at the top (keep the existing ones):

```python
import re
from dataclasses import dataclass, replace
from typing import Any, Optional

from tests.automation.backtest_judge_fixtures import NOW, Clock
from trader.automation.authored_config import AuthoredStrategiesConfig
from trader.automation.authored_files import AuthoredFileStore
from trader.automation.authored_strategies import AuthoredStrategyStore
from trader.automation.authored_wire import SubmitAuthoredStrategyRequest
from trader.automation.calendar_policy import XNYSCalendarPolicy
```

and append:

```python
def submit_request(text: Optional[str] = None, **changes: Any) -> SubmitAuthoredStrategyRequest:
    text = text or VALID_SOURCE
    found = re.search(r"RANGE_BARS = (\d+)", text)
    raw = {"source": text, "tunables": tunables_for(int(found.group(1)) if found else 15), "bar_size": "5 mins",
           "note": "breaks above the range of the last bars", "author_model": "openrouter/author-1"}
    raw.update(changes)
    return SubmitAuthoredStrategyRequest.model_validate(raw)


@dataclass
class AuthoredWorld:
    db: DuckDBConnection
    clock: Clock
    store: AuthoredStrategyStore
    tmp_path: Path


def _store(db, clock, tmp_path, *, lines=None, calendar=None, proofs=None, withdraw=None,
           **config) -> AuthoredStrategyStore:
    return AuthoredStrategyStore(db, config=replace(AuthoredStrategiesConfig(enabled=True), **config),
                                 files=AuthoredFileStore(tmp_path / "store"), now=clock, proofs=proofs,
                                 calendar=calendar or XNYSCalendarPolicy(), shadow_sessions=20, lines=lines,
                                 withdraw=withdraw)


def authored_world(tmp_path: Path, *, lines=None, calendar=None, proofs=None, withdraw=None,
                   **config) -> AuthoredWorld:
    db, clock = authored_journal(tmp_path), Clock(NOW)
    return AuthoredWorld(db, clock, _store(db, clock, tmp_path, lines=lines, calendar=calendar, proofs=proofs,
                                           withdraw=withdraw, **config), tmp_path)


def with_config(world: AuthoredWorld, **config) -> AuthoredStrategyStore:
    """Another store on the same journal and files, as after a trader restart with an edited trader.yaml."""
    return _store(world.db, world.clock, world.tmp_path, **config)


def stored(world: AuthoredWorld, range_bars: int = 15) -> str:
    reply = world.store.submit(submit_request(source(range_bars)))
    assert reply["status"] == "STORED", reply
    return reply["record"]["digest"]


def stored_files(world: AuthoredWorld) -> list[Path]:
    root = world.tmp_path / "store"
    return sorted(root.glob("*.py")) if root.exists() else []
```

- [ ] **Step 2: Write the failing tests** (`tests/automation/test_authored_submission.py`)

```python
"""SP3 Plan 3 Task 5: submit_authored_strategy (spec 7.3, 8, 10 Authority and Cap; rulings 5-6)."""
from __future__ import annotations

import datetime as dt
import json
import threading

import pytest
from pydantic import ValidationError

from tests.automation.authored_fixtures import (
    TUNABLES, VALID_SOURCE, authored_world, digest_of, source, stored, stored_files, submit_request, with_config,
)
from tests.automation.backtest_judge_fixtures import NOW
from trader.automation.authored_strategies import AuthoredStrategyStore


def test_a_clean_file_is_stored_with_its_metadata(tmp_path):
    w = authored_world(tmp_path)
    reply = w.store.submit(submit_request())
    assert (reply["status"], reply["code"]) == ("STORED", None)
    record, digest = reply["record"], digest_of(VALID_SOURCE)
    assert (record["digest"], record["key"]) == (digest, f"authored:{digest}:RangeBreak")
    assert (record["state"], record["attempts"], record["bar_size"], record["reason"]) == ("STORED", 0, "5 mins", None)
    assert record["tunables"] == TUNABLES
    hex_digest = digest.removeprefix("sha256:")
    assert (tmp_path / "store" / f"{hex_digest}.py").read_bytes() == VALID_SOURCE.encode("utf-8")
    meta = json.loads((tmp_path / "store" / f"{hex_digest}.json").read_text())
    assert (meta["class_name"], meta["bar_size"], meta["author_model"], meta["tunables"]) == (
        "RangeBreak", "5 mins", "openrouter/author-1", TUNABLES)


def test_a_retry_after_a_lost_reply_is_existing_even_with_a_full_cap_and_the_switch_off(tmp_path):  # focus 2
    w = authored_world(tmp_path, max_active=1)
    first = w.store.submit(submit_request())
    again = with_config(w, enabled=False, max_active=1).submit(submit_request(note="another note"))
    assert again["status"] == "EXISTING"
    assert again["record"] == first["record"]                         # ruling 6: the stored note wins


def test_the_switch_off_refuses_and_writes_nothing(tmp_path):              # spec 10 Authority
    w = authored_world(tmp_path, enabled=False)
    reply = w.store.submit(submit_request())
    assert (reply["status"], reply["code"], reply["record"]) == ("REFUSED", "AUTHORED_STRATEGIES_DISABLED", None)
    assert stored_files(w) == []


def test_a_full_cap_refuses_and_writes_nothing(tmp_path):                  # spec 10 Cap
    w = authored_world(tmp_path, max_active=2)
    stored(w, 10)
    stored(w, 11)
    reply = w.store.submit(submit_request(source(12)))
    assert (reply["status"], reply["code"]) == ("REFUSED", "AUTHORED_CAP_FULL")
    assert len(stored_files(w)) == 2


def test_two_submits_for_the_last_slot_store_exactly_one(tmp_path):        # review focus 1
    w = authored_world(tmp_path, max_active=1)
    barrier, outcomes = threading.Barrier(2), []

    def attempt(range_bars: int) -> None:
        barrier.wait()
        reply = w.store.submit(submit_request(source(range_bars)))
        outcomes.append(reply["code"] or reply["status"])
    threads = [threading.Thread(target=attempt, args=(n,)) for n in (20, 21)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
    assert sorted(outcomes) == ["AUTHORED_CAP_FULL", "STORED"]
    assert w.store.overview()["counted"] == 1


def test_a_file_the_static_check_refuses_is_rejected_with_its_rule(tmp_path):
    w = authored_world(tmp_path)
    reply = w.store.submit(submit_request(VALID_SOURCE.replace("import pandas as pd", "import os")))
    assert (reply["status"], reply["code"]) == ("REFUSED", "AUTHORED_STATIC_REJECTED")
    assert "@" in reply["detail"]                                     # rule@line: detail
    assert stored_files(w) == []


def test_a_bar_size_outside_the_owner_list_is_refused(tmp_path):
    w = authored_world(tmp_path, bar_sizes=("1 min",))
    assert w.store.submit(submit_request())["code"] == "AUTHORED_BAD_BAR_SIZE"


@pytest.mark.parametrize("tunables, hint", [
    ({"WINDOW": {"low": 1, "high": 2, "default": 1}}, "not a numeric class constant"),
    ({"LOOKBACK_BARS": {"low": 10, "high": 50, "default": 40}}, "fixed by the API"),
    ({"RANGE_BARS": {"low": 30, "high": 5, "default": 15}}, "not a finite, ordered range"),
    ({"RANGE_BARS": {"low": float("nan"), "high": 30, "default": 15}}, "not a finite, ordered range"),
    ({"RANGE_BARS": {"low": 20, "high": 30, "default": 15}}, "the class value 15 is outside [20, 30]"),
    ({"RANGE_BARS": {"low": 5, "high": 30, "default": 16}}, "the default 16 is not the class value 15"),
    ({"range_bars": {"low": 5, "high": 30, "default": 15}}, "not an upper-case tunable name"),
    ({}, "at least one tunable"),
])
def test_bad_tunables_are_refused_with_the_reason(tmp_path, tunables, hint):
    w = authored_world(tmp_path)
    reply = w.store.submit(submit_request(tunables=tunables))
    assert reply["code"] == "AUTHORED_BAD_TUNABLES"
    assert hint in reply["detail"]
    assert stored_files(w) == []


def test_a_bool_bound_or_too_many_tunables_never_pass_the_wire():
    with pytest.raises(ValidationError):
        submit_request(tunables={"RANGE_BARS": {"low": True, "high": 30, "default": 15}})
    with pytest.raises(ValidationError):
        submit_request(tunables={"RANGE_BARS": {"low": 5, "high": 30}})            # a default is required (D5)
    with pytest.raises(ValidationError):
        submit_request(tunables={f"T{i}": {"low": 0, "high": 1, "default": 0} for i in range(33)})
    with pytest.raises(ValidationError):
        submit_request(note="x" * 601)


def test_a_crash_after_the_file_leaves_an_orphan_that_the_next_submit_records(tmp_path, monkeypatch):  # focus 3
    w = authored_world(tmp_path)

    def trader_dies(self, conn, record):
        raise RuntimeError("the trader died before the record")
    monkeypatch.setattr(AuthoredStrategyStore, "_insert_in_tx", trader_dies)
    with pytest.raises(RuntimeError):
        w.store.submit(submit_request())
    assert len(stored_files(w)) == 1 and w.store.get(digest_of(VALID_SOURCE)) is None
    monkeypatch.undo()
    assert w.store.submit(submit_request())["status"] == "STORED"


def test_the_overview_counts_the_cap_and_lists_oldest_first(tmp_path):
    w = authored_world(tmp_path, max_active=3)
    first = stored(w, 10)
    w.clock.now = NOW + dt.timedelta(minutes=1)
    second = stored(w, 11)
    view = w.store.overview()
    assert (view["enabled"], view["max_active"], view["counted"], view["room"], view["judged_count"]) == (
        True, 3, 2, 1, 0)
    assert [record["digest"] for record in view["records"]] == [first, second]
    assert view["bar_sizes"] == ["1 min", "5 mins", "15 mins"]
```

- [ ] **Step 3: Run the tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/automation/test_authored_submission.py -q --timeout=60`
Expected: FAIL (`ModuleNotFoundError: trader.automation.authored_wire`).

- [ ] **Step 4: Write the wire models** (`trader/automation/authored_wire.py`)

```python
"""Typed RPC bodies of the SP3 authored-strategy methods (spec 5, 7.3, 7.9; Plan 3 Cross-plan additions).

Strict: unknown keys, missing keys and coercions are refused (VALIDATION_ERROR). Business rules that the
author must learn from (tunable names and ranges, the bar size, the static check) are checked by the
store and answered as refusals, not here.
"""
from __future__ import annotations

import datetime as dt
import re
from typing import Literal, Optional, Union

from pydantic import (
    BaseModel, ConfigDict, Field, StrictBool, StrictFloat, StrictInt, ValidationInfo, field_validator,
    model_validator,
)

from trader.automation.backtest_judge_wire import DIGEST, JEV_MODEL, parse_aware
from trader.research.request_rules import MAX_PARAMS

MAX_SOURCE_CHARS = 20_480
MAX_NOTE_CHARS = 600
MAX_DETAIL_CHARS = 500
MAX_CHECKS = 64
CHECK_NAME = re.compile(r"^[a-z0-9_.:-]{1,64}$")
# Plan 2 ruling 1: the sandbox image's identity is its build id (<git sha>-<UTC time>), baked into the image.
IMAGE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:+-]{0,127}$")
Number = Union[StrictInt, StrictFloat]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


def _digest(name: str, value: str) -> str:
    if not DIGEST.fullmatch(value):
        raise ValueError(f"{name} must be sha256:<64 hex>")
    return value


def _image_id(name: str, value: str) -> str:
    if value == "unset" or not IMAGE_ID.fullmatch(value):
        raise ValueError(f"{name} must be a build id matching {IMAGE_ID.pattern}, never 'unset'")
    return value


class TunableRange(_Strict):
    low: Number
    high: Number
    default: Number          # equals the class constant (ruling 5, decision D5)


class SubmitAuthoredStrategyRequest(_Strict):
    source: str = Field(min_length=1, max_length=MAX_SOURCE_CHARS)
    tunables: dict[str, TunableRange] = Field(max_length=MAX_PARAMS)
    bar_size: str = Field(min_length=1, max_length=16)
    note: str = Field(max_length=MAX_NOTE_CHARS)
    author_model: str

    @field_validator("author_model")
    @classmethod
    def _author_model(cls, value: str) -> str:
        if not JEV_MODEL.fullmatch(value):
            raise ValueError(f"author_model must match {JEV_MODEL.pattern}")
        return value


class GetAuthoredStrategiesRequest(_Strict):
    """No fields: the reply is the whole list, which the cap keeps small."""


class ReportAuthoredEvaluationRequest(_Strict):
    digest: str
    request_id: str
    event: Literal["STARTED", "SANDBOX_UNAVAILABLE", "SANDBOX_ERROR"]
    sandbox_image_digest: Optional[str]
    detail: str = Field(max_length=MAX_DETAIL_CHARS)

    @field_validator("digest", "request_id")
    @classmethod
    def _digests(cls, value: str, info: ValidationInfo) -> str:
        return _digest(info.field_name, value)

    @model_validator(mode="after")
    def _image(self) -> "ReportAuthoredEvaluationRequest":
        if (self.event == "STARTED") != (self.sandbox_image_digest is not None):
            raise ValueError("sandbox_image_digest is required for STARTED and null for every other event")
        if self.sandbox_image_digest is not None:
            _image_id("sandbox_image_digest", self.sandbox_image_digest)
        return self


class RetireAuthoredStrategyRequest(_Strict):
    digest: str
    code: Literal["SANDBOX_ERROR", "SANDBOX_DIGEST_MISMATCH"]
    detail: str = Field(max_length=MAX_DETAIL_CHARS)

    @field_validator("digest")
    @classmethod
    def _digest_field(cls, value: str) -> str:
        return _digest("digest", value)


class IsolationCheck(_Strict):
    name: str
    passed: StrictBool
    detail: str = Field(max_length=MAX_DETAIL_CHARS)

    @field_validator("name")
    @classmethod
    def _name(cls, value: str) -> str:
        if not CHECK_NAME.fullmatch(value):
            raise ValueError(f"a check name must match {CHECK_NAME.pattern}")
        return value


class RecordIsolationProofRequest(_Strict):
    image_digest: str
    passed: StrictBool
    checks: list[IsolationCheck] = Field(min_length=1, max_length=MAX_CHECKS)
    ran_at: str

    @field_validator("image_digest")
    @classmethod
    def _image(cls, value: str) -> str:
        return _image_id("image_digest", value)

    @field_validator("ran_at")
    @classmethod
    def _ran_at(cls, value: str) -> str:
        parse_aware(value)
        return value

    @model_validator(mode="after")
    def _consistent(self) -> "RecordIsolationProofRequest":
        names = [check.name for check in self.checks]
        if len(set(names)) != len(names):
            raise ValueError("check names must be distinct")
        if self.passed != all(check.passed for check in self.checks):
            raise ValueError("passed must equal every check passing (Plan 3 ruling 21)")
        return self

    def ran_at_utc(self) -> dt.datetime:
        return parse_aware(self.ran_at)


class GetIsolationProofRequest(_Strict):
    image_digest: Optional[str]

    @field_validator("image_digest")
    @classmethod
    def _image(cls, value: Optional[str]) -> Optional[str]:
        return None if value is None else _image_id("image_digest", value)
```

- [ ] **Step 5: Write the submission checks** (`trader/automation/authored_submission.py`)

```python
"""Checks of one submit_authored_strategy body before anything is written (SP3 spec 7.3; Plan 3 ruling 5).

Pure: no file and no journal. The order follows spec 7.3: the static check, the bar size, the tunables
(the store checks the switch and the cap first). The trader reads the file with ``ast`` and Plan 1's
checker only; it never imports or runs it.
"""
from __future__ import annotations

import ast
import math
from dataclasses import dataclass
from typing import Mapping, Optional

from trader.authored.static_check import AuthoredStaticRejected, require_clean
from trader.automation.authored_config import AuthoredStrategiesConfig
from trader.automation.authored_refusals import (
    AUTHORED_BAD_BAR_SIZE, AUTHORED_BAD_TUNABLES, AUTHORED_STATIC_REJECTED, AuthoredRefused,
)
from trader.automation.authored_wire import SubmitAuthoredStrategyRequest, TunableRange
from trader.automation.strategy_binding import bar_size_key
from trader.research.request_rules import TUNABLE_NAME

NOT_TUNABLE = frozenset({"LOOKBACK_BARS"})     # fixed by the API (spec 7.1)
MAX_STATIC_DETAIL_CHARS = 2000


@dataclass(frozen=True)
class CheckedSubmission:
    class_name: str
    bar_size: str
    tunables: dict[str, dict[str, float]]


def check_submission(request: SubmitAuthoredStrategyRequest, config: AuthoredStrategiesConfig) -> CheckedSubmission:
    class_name = _static_class(request.source)
    bar_size = _bar_size(request.bar_size, config)
    return CheckedSubmission(class_name, bar_size, _tunables(request.source, class_name, request.tunables))


def _static_class(source: str) -> str:
    try:
        return require_clean(source)
    except AuthoredStaticRejected as rejected:
        detail = "; ".join(f"{v.rule}@{v.line}: {v.detail}" for v in rejected.violations)
        raise AuthoredRefused(AUTHORED_STATIC_REJECTED,
                              detail[:MAX_STATIC_DETAIL_CHARS] or "the static check refused the file") from None


def _bar_size(bar_size: str, config: AuthoredStrategiesConfig) -> str:
    if bar_size_key(bar_size) not in {bar_size_key(size) for size in config.bar_sizes}:
        raise AuthoredRefused(AUTHORED_BAD_BAR_SIZE, f"{bar_size!r} is not one of {list(config.bar_sizes)}")
    return bar_size


def _tunables(source: str, class_name: str, tunables: Mapping[str, TunableRange]) -> dict[str, dict[str, float]]:
    if not tunables:
        raise AuthoredRefused(AUTHORED_BAD_TUNABLES, "an authored file needs at least one tunable: SP2c varies one "
                                                     "for its neighbour points")
    constants = class_constants(source, class_name)
    checked = {}
    for name in sorted(tunables):
        bounds = tunables[name]
        problem = _tunable_problem(name, bounds.low, bounds.high, bounds.default, constants)
        if problem is not None:
            raise AuthoredRefused(AUTHORED_BAD_TUNABLES, problem)
        checked[name] = {"low": bounds.low, "high": bounds.high, "default": bounds.default}
    return checked


def _tunable_problem(name: str, low: float, high: float, default: float,
                     constants: Mapping[str, float]) -> Optional[str]:
    if not TUNABLE_NAME.fullmatch(name):
        return f"{name!r} is not an upper-case tunable name"
    if name in NOT_TUNABLE:
        return f"{name} is fixed by the API, not a tunable"
    if name not in constants:
        return f"{name} is not a numeric class constant of the strategy"
    if not (math.isfinite(low) and math.isfinite(high)) or low > high:
        return f"{name}: [{low}, {high}] is not a finite, ordered range"
    if not low <= constants[name] <= high:
        return f"{name}: the class value {constants[name]} is outside [{low}, {high}]"
    if default != constants[name]:
        return f"{name}: the default {default} is not the class value {constants[name]}"
    return None


def class_constants(source: str, class_name: str) -> dict[str, float]:
    """Upper-case class-level constants of ``class_name`` that are plain numbers (a bool is not one)."""
    for node in ast.parse(source).body:
        if isinstance(node, ast.ClassDef) and node.name == class_name:
            pairs = (_constant(statement) for statement in node.body)
            return {name: value for name, value in pairs if name is not None}
    return {}


def _constant(statement: ast.stmt) -> tuple[Optional[str], Optional[float]]:
    if isinstance(statement, ast.Assign) and len(statement.targets) == 1 \
            and isinstance(statement.targets[0], ast.Name):
        name, value = statement.targets[0].id, statement.value
    elif isinstance(statement, ast.AnnAssign) and isinstance(statement.target, ast.Name) \
            and statement.value is not None:
        name, value = statement.target.id, statement.value
    else:
        return None, None
    number = _number(value)
    if number is None or not TUNABLE_NAME.fullmatch(name):
        return None, None
    return name, number


def _number(node: ast.expr) -> Optional[float]:
    sign = 1
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.USub, ast.UAdd)):
        sign = -1 if isinstance(node.op, ast.USub) else 1
        node = node.operand
    if isinstance(node, ast.Constant) and type(node.value) in (int, float) and math.isfinite(node.value):
        return sign * node.value
    return None
```

- [ ] **Step 6: Add the store with `submit` and `overview`** (append to `trader/automation/authored_strategies.py`)

Add these imports to the top of the file:

```python
from typing import Callable

from trader.authored.keys import source_digest
from trader.automation.authored_config import AuthoredStrategiesConfig
from trader.automation.authored_files import AuthoredFileStore
from trader.automation.authored_refusals import AUTHORED_CAP_FULL, AUTHORED_STRATEGIES_DISABLED
from trader.automation.authored_submission import check_submission
from trader.automation.authored_wire import SubmitAuthoredStrategyRequest
```

and append:

```python
def _reply(status: str, record: AuthoredRecord, **extra: Any) -> dict:
    return {"status": status, "code": None, "detail": None, "retryable": False, "record": record.to_json(),
            **extra}


class AuthoredStrategyStore:
    """The trader's authority over authored strategies (SP3 spec 7.3). One instance per trader."""

    def __init__(self, db: Any, *, config: AuthoredStrategiesConfig, files: AuthoredFileStore,
                 now: Callable[[], dt.datetime], proofs: Any = None, calendar: Any = None,
                 shadow_sessions: int = 20, lines: Optional[Callable[[], Any]] = None,
                 withdraw: Optional[Callable[[str], bool]] = None):
        """``proofs`` (IsolationProofs) gates registration; ``calendar``, ``shadow_sessions`` and ``lines``
        (a callable returning DeploymentLines) drive the lazy expiry of ruling 11; ``withdraw`` (the SP2c
        version withdrawal, Task 6) ends a retired record's deployment line (ruling 18, decision D4)."""
        self._db = db
        self._config = config
        self._files = files
        self._now = now
        self._proofs = proofs
        self._calendar = calendar
        self._shadow_sessions = shadow_sessions
        self._lines = lines
        self._withdraw = withdraw

    @property
    def config(self) -> AuthoredStrategiesConfig:
        return self._config

    def get(self, digest: str) -> Optional[AuthoredRecord]:
        return self._db.transaction(lambda conn: record_in_tx(conn, digest))

    def submit(self, request: SubmitAuthoredStrategyRequest) -> dict:
        source = request.source.encode("utf-8")
        digest = source_digest(source)
        existing = self.get(digest)
        if existing is not None:                  # ruling 6: before the switch, the cap and the checks
            return _reply("EXISTING", existing)
        try:
            self._require_enabled()
            self._require_room()
            checked = check_submission(request, self._config)
            record = new_record(digest=digest, class_name=checked.class_name, bar_size=checked.bar_size,
                                tunables=checked.tunables, note=request.note, author_model=request.author_model,
                                now=self._now())
            self._files.write(digest, source, record.file_metadata())      # the file before the record
            status, saved = self._db.transaction(lambda conn: self._insert_in_tx(conn, record))
        except AuthoredRefused as refused:
            return refused.reply()
        return _reply(status, saved)

    def overview(self) -> dict:
        """``get_authored_strategies`` and ``list_authored_strategies``: every record, the cap and the switch."""
        records, judged = self._db.transaction(lambda conn: (records_in_tx(conn), judged_count_in_tx(conn)))
        counted = sum(1 for record in records if record.state in COUNTED_STATES)
        return {"enabled": self._config.enabled, "max_active": self._config.max_active, "counted": counted,
                "room": max(0, self._config.max_active - counted), "judged_count": judged,
                "bar_sizes": list(self._config.bar_sizes), "records": [record.to_json() for record in records]}

    def _require_enabled(self) -> None:
        if not self._config.enabled:
            raise AuthoredRefused(AUTHORED_STRATEGIES_DISABLED, "ai_paper.authored_strategies.enabled is false")

    def _require_room(self) -> None:
        """A cheap check before the static check and the file write; the insert counts again."""
        counted = self._db.transaction(counted_in_tx)
        if counted >= self._config.max_active:
            raise AuthoredRefused(AUTHORED_CAP_FULL, self._cap_detail(counted))

    def _insert_in_tx(self, conn: Any, record: AuthoredRecord) -> tuple[str, AuthoredRecord]:
        raced = record_in_tx(conn, record.digest)
        if raced is not None:                     # the same file, submitted twice at once
            return "EXISTING", raced
        counted = counted_in_tx(conn)
        if counted >= self._config.max_active:
            raise AuthoredRefused(AUTHORED_CAP_FULL, self._cap_detail(counted))
        insert_in_tx(conn, record)
        return "STORED", record

    def _cap_detail(self, counted: int) -> str:
        return f"{counted} of {self._config.max_active} authored strategies are STORED, EVALUATING, SHADOW or DEPLOYED"
```

`AuthoredStoreError` from the file store is not caught here: an unusable store directory fails loudly as an RPC error (Task 9 maps it).

- [ ] **Step 7: Run the tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/automation/test_authored_submission.py tests/automation/test_authored_records.py tests/automation/test_authored_files.py -q --timeout=60`
Expected: PASS.

- [ ] **Step 8: Commit**

```bash
git add trader/automation/authored_wire.py trader/automation/authored_submission.py \
  trader/automation/authored_strategies.py tests/automation/authored_fixtures.py \
  tests/automation/test_authored_submission.py
git commit -m "feat: store an authored strategy file and its record under the cap

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---
### Task 6: Research reports, retirement and lazy expiry

**Files:**
- Modify: `trader/automation/authored_strategies.py` (add `report`, `retire`, `refresh`, `DeploymentLines`), `tests/automation/authored_fixtures.py`
- Test: `tests/automation/test_authored_lifecycle.py`

**Interfaces:**
- Consumes: Task 4 core and Task 5 store; `ReportAuthoredEvaluationRequest`, `RetireAuthoredStrategyRequest`; `nth_session_after`, `JudgmentRefused` (`trader/automation/backtest_judgments.py`); `ny_day` (`trader/automation/evaluation_claims.py`); `EXPIRED`, `JUDGMENT_ENDED`, `SUPERSEDED`, `WITHDRAWN` (`trader/automation/ai_deployment_activity.py`); `DeploymentRefused` (`trader/automation/ai_deployments.py`), `WITHDRAWAL_ENTRY_IN_FLIGHT` and `AiDeploymentVersionStore.withdraw(digest, *, reason, principal, command_id) -> bool` (`trader/automation/ai_deployment_versions.py`, the SP2c withdraw path).
- Produces: `AuthoredStrategyStore.report(request) -> dict`, `.retire(request) -> dict` (moves the record, then withdraws its version: decision D4), `AUTHORED_RETIRE_REASON`, `authored_retire_command_id(version_digest) -> str`, `withdraw_with(versions) -> Callable[[str], bool]`, `.refresh() -> None` (now called first by `submit` and `overview`); `DeploymentLines(*, activity, versions)` with `.end_status(version_digest) -> str | None`. Fixtures `evaluating(world, digest, request_id)`, `moved(world, digest, target, **changes)`, `report(world, digest, request_id, event, image=None) -> dict`, `deployed(world, range_bars=15, version=...) -> str`.

- [ ] **Step 1: Add the fixtures** (append to `tests/automation/authored_fixtures.py`)

Add the imports:

```python
from trader.automation.authored_strategies import (
    DEPLOYED, EVALUATING, SHADOW, AuthoredRecord, AuthoredState, move_in_tx, record_in_tx,
)
from trader.automation.authored_wire import ReportAuthoredEvaluationRequest
```

and append:

```python
VERSION = "sha256:" + "7" * 64


def evaluating(world: AuthoredWorld, digest: str, request_id_: str) -> AuthoredRecord:
    """What an accepted SP2c claim does (Task 7): STORED -> EVALUATING on ``request_id_``."""
    return world.db.transaction(lambda conn: move_in_tx(
        conn, record_in_tx(conn, digest), EVALUATING, event="CLAIMED", reason=None, now=world.clock(),
        request_id=request_id_, evaluation_request_id=request_id_))


def moved(world: AuthoredWorld, digest: str, target: AuthoredState, *, reason: Optional[str] = None,
          **changes: Any) -> AuthoredRecord:
    return world.db.transaction(lambda conn: move_in_tx(
        conn, record_in_tx(conn, digest), target, event="TEST", reason=reason, now=world.clock(), **changes))


def report(world: AuthoredWorld, digest: str, request_id_: str, event: str, image: Optional[str] = None) -> dict:
    return world.store.report(ReportAuthoredEvaluationRequest.model_validate({
        "digest": digest, "request_id": request_id_, "event": event, "sandbox_image_digest": image,
        "detail": ""}))


def deployed(world: AuthoredWorld, range_bars: int = 15, version: str = VERSION) -> str:
    """A record judged DEPLOY and registered as ``version`` (what Tasks 7 and 8 do through SP2c)."""
    digest = stored(world, range_bars)
    evaluating(world, digest, request_id(range_bars))
    moved(world, digest, SHADOW, reason="AWAITING_REGISTRATION", verdict="DEPLOY", judgment_id="jdg-00000001",
          evaluation_request_id=None)
    moved(world, digest, DEPLOYED, deployment_version=version)
    return digest
```

- [ ] **Step 2: Write the failing tests** (`tests/automation/test_authored_lifecycle.py`)

```python
"""SP3 Plan 3 Task 6: research reports, retirement and lazy expiry (spec 7.3, 7.6, 8, 10 Cap; rulings 11, 17-18)."""
from __future__ import annotations

import datetime as dt
from types import SimpleNamespace

import pytest

from tests.automation.authored_fixtures import (
    IMAGE, VERSION, authored_world, deployed, evaluating, moved, report, request_id, source, stored,
    submit_request,
)
from tests.automation.backtest_judge_fixtures import NOW
from trader.automation.ai_deployment_versions import WITHDRAWAL_ENTRY_IN_FLIGHT
from trader.automation.ai_deployments import DeploymentRefused
from trader.automation.authored_strategies import (
    AUTHORED_RETIRE_REASON, SHADOW, DeploymentLines, authored_retire_command_id, withdraw_with,
)
from trader.automation.authored_wire import RetireAuthoredStrategyRequest

ZERO = "sha256:" + "0" * 64


def retire_request(digest: str, code: str = "SANDBOX_ERROR") -> RetireAuthoredStrategyRequest:
    return RetireAuthoredStrategyRequest.model_validate({"digest": digest, "code": code, "detail": "live fault"})


def test_started_records_the_sandbox_image_and_a_repeat_is_unchanged(tmp_path):
    w = authored_world(tmp_path)
    digest = stored(w)
    evaluating(w, digest, request_id(1))
    first = report(w, digest, request_id(1), "STARTED", IMAGE)
    assert (first["status"], first["record"]["state"], first["record"]["sandbox_image_digest"]) == (
        "UPDATED", "EVALUATING", IMAGE)
    assert first["judged_count"] == 0
    assert first["record"]["tunables"]["RANGE_BARS"] == {"low": 5, "high": 30, "default": 15}  # cohort point 0
    again = report(w, digest, request_id(1), "STARTED", IMAGE)
    assert (again["status"], len(again["record"]["history"])) == ("UNCHANGED", len(first["record"]["history"]))


def test_a_report_must_name_the_records_current_request(tmp_path):                        # ruling 17
    w = authored_world(tmp_path)
    digest = stored(w)
    assert report(w, digest, request_id(1), "SANDBOX_ERROR")["code"] == "AUTHORED_STATE_CONFLICT"   # not claimed
    evaluating(w, digest, request_id(1))
    assert report(w, digest, request_id(2), "SANDBOX_ERROR")["code"] == "AUTHORED_STATE_CONFLICT"
    assert report(w, ZERO, request_id(1), "SANDBOX_ERROR")["code"] == "AUTHORED_STRATEGY_UNKNOWN"
    assert w.store.get(digest).state.value == "EVALUATING"


def test_a_sandbox_error_rejects_the_record_and_frees_the_cap(tmp_path):
    w = authored_world(tmp_path, max_active=1)
    digest = stored(w)
    evaluating(w, digest, request_id(1))
    reply = report(w, digest, request_id(1), "SANDBOX_ERROR")
    assert (reply["record"]["state"], reply["record"]["reason"]) == ("REJECTED", "SANDBOX_ERROR")
    assert w.store.submit(submit_request(source(16)))["status"] == "STORED"


def test_a_repeated_unavailable_report_counts_once(tmp_path):                                # review focus 4
    w = authored_world(tmp_path)
    digest = stored(w)
    evaluating(w, digest, request_id(1))
    first = report(w, digest, request_id(1), "SANDBOX_UNAVAILABLE")
    again = report(w, digest, request_id(1), "SANDBOX_UNAVAILABLE")
    assert (first["status"], first["record"]["state"], first["record"]["attempts"]) == ("UPDATED", "STORED", 1)
    assert (again["status"], again["record"]["attempts"]) == ("UNCHANGED", 1)
    assert first["record"]["evaluation_request_id"] is None


def test_three_unavailable_nights_fail_the_record_and_free_the_cap(tmp_path):          # focus 4, spec 10 Cap
    w = authored_world(tmp_path, max_active=3)
    digests = [stored(w, n) for n in (10, 11, 12)]
    assert w.store.submit(submit_request(source(13)))["code"] == "AUTHORED_CAP_FULL"
    for night in range(3):
        for index, digest in enumerate(digests):
            claim = request_id(100 * night + index + 1)
            evaluating(w, digest, claim)                 # a STORED record is evaluated again the next night
            reply = report(w, digest, claim, "SANDBOX_UNAVAILABLE")
            assert reply["record"]["state"] == ("FAILED" if night == 2 else "STORED")
    assert {w.store.get(digest).reason for digest in digests} == {"SANDBOX_UNAVAILABLE"}
    assert w.store.submit(submit_request(source(13)))["status"] == "STORED"


class FakeWithdrawals:
    """The SP2c withdraw path as the store sees it: True when this call withdrew, False when done before."""

    def __init__(self, refuse_first: int = 0):
        self.calls, self.withdrawn, self.refuse_first = [], set(), refuse_first

    def __call__(self, version_digest):
        self.calls.append(version_digest)
        if self.refuse_first:
            self.refuse_first -= 1
            raise DeploymentRefused(WITHDRAWAL_ENTRY_IN_FLIGHT, "an entry of this version is being sent")
        newly = version_digest not in self.withdrawn
        self.withdrawn.add(version_digest)
        return newly


def test_the_strategy_retires_a_deployed_record_once_and_withdraws_its_version(tmp_path):  # ruling 18, D4
    withdrawals = FakeWithdrawals()
    w = authored_world(tmp_path, withdraw=withdrawals)
    digest = deployed(w)
    first = w.store.retire(retire_request(digest, "SANDBOX_DIGEST_MISMATCH"))
    assert (first["status"], first["record"]["state"], first["record"]["reason"]) == ("UPDATED", "RETIRED",
                                                                                     "SANDBOX_ERROR")
    assert first["record"]["history"][-1]["detail"].startswith("SANDBOX_DIGEST_MISMATCH")
    assert withdrawals.withdrawn == {VERSION}
    assert w.store.retire(retire_request(digest))["status"] == "UNCHANGED"
    assert withdrawals.calls == [VERSION, VERSION]                # the repeat re-checks the withdrawal (a no-op)
    assert w.store.overview()["counted"] == 0                     # spec 10 Cap: RETIRED is not counted


def test_an_entry_in_flight_keeps_the_withdrawal_retryable_and_the_record_retired(tmp_path):   # D4
    withdrawals = FakeWithdrawals(refuse_first=1)
    w = authored_world(tmp_path, withdraw=withdrawals)
    digest = deployed(w)
    first = w.store.retire(retire_request(digest))
    assert (first["status"], first["code"], first["retryable"]) == ("REFUSED", "WITHDRAWAL_ENTRY_IN_FLIGHT", True)
    assert first["record"]["state"] == "RETIRED"                  # entries are refused from here on (ruling 16)
    again = w.store.retire(retire_request(digest))
    assert again["status"] == "UNCHANGED" and withdrawals.withdrawn == {VERSION}


def test_the_withdrawal_uses_the_sp2c_path_with_a_stable_command_id():
    seen = []
    versions = SimpleNamespace(withdraw=lambda digest, **kw: seen.append((digest, kw)) or True)
    assert withdraw_with(versions)(VERSION) is True
    assert seen == [(VERSION, {"reason": AUTHORED_RETIRE_REASON, "principal": "strategy",
                               "command_id": authored_retire_command_id(VERSION)})]
    assert authored_retire_command_id(VERSION) == authored_retire_command_id(VERSION)


def test_retire_refuses_a_record_that_is_not_deployed(tmp_path):
    w = authored_world(tmp_path)
    digest = stored(w)
    assert w.store.retire(retire_request(digest))["code"] == "AUTHORED_STATE_CONFLICT"
    assert w.store.retire(retire_request(ZERO))["code"] == "AUTHORED_STRATEGY_UNKNOWN"


def test_a_shadow_record_expires_after_the_deploy_window(tmp_path):                       # ruling 11
    w = authored_world(tmp_path)
    digest = stored(w)
    evaluating(w, digest, request_id(1))
    moved(w, digest, SHADOW, reason="JEV_SHADOW", verdict="SHADOW", judgment_id="jdg-00000001")
    w.clock.now = NOW + dt.timedelta(days=20)        # 2026-10-28: fewer than 20 sessions after 2026-10-08
    assert w.store.overview()["records"][0]["state"] == "SHADOW"
    w.clock.now = NOW + dt.timedelta(days=40)        # 2026-11-17: after the 20th session (2026-11-05)
    view = w.store.overview()
    assert (view["records"][0]["state"], view["records"][0]["reason"], view["counted"]) == (
        "EXPIRED", "SHADOW_EXPIRED", 0)


class NoSessions:
    def sessions_in_range(self, start, end):
        return []


def test_a_calendar_that_cannot_serve_keeps_the_record_counted(tmp_path):
    w = authored_world(tmp_path, calendar=NoSessions())
    digest = stored(w)
    evaluating(w, digest, request_id(1))
    moved(w, digest, SHADOW, reason="JEV_SHADOW", verdict="SHADOW", judgment_id="jdg-00000001")
    w.clock.now = NOW + dt.timedelta(days=400)
    assert w.store.overview()["counted"] == 1


class FakeLines:
    def __init__(self, status):
        self.status = status

    def end_status(self, version_digest):
        return self.status if version_digest == VERSION else None


@pytest.mark.parametrize("status, state, reason", [
    ("WITHDRAWN", "RETIRED", "OWNER_RETIRED"),
    ("EXPIRED", "EXPIRED", "DEPLOY_EXPIRED"),
    ("JUDGMENT_ENDED", "EXPIRED", "RENEWAL_ENDED"),
    ("ACTIVE", "DEPLOYED", None),
    ("OVER_CAP", "DEPLOYED", None),
    (None, "DEPLOYED", None),
])
def test_a_deployed_record_follows_its_version_line(tmp_path, status, state, reason):
    lines = FakeLines(status)
    w = authored_world(tmp_path, lines=lambda: lines)
    deployed(w)
    record = w.store.overview()["records"][0]
    assert (record["state"], record["reason"]) == (state, reason)


def test_the_line_end_follows_renewals_to_the_last_version():
    def sealed(digest, prior):
        return SimpleNamespace(digest=digest, version=SimpleNamespace(prior_version=prior))
    statuses = {"v1": "SUPERSEDED", "v2": "SUPERSEDED", "v3": "WITHDRAWN"}
    lines = DeploymentLines(activity=SimpleNamespace(statuses=lambda: statuses),
                            versions=SimpleNamespace(sealed=lambda: (sealed("v1", None), sealed("v2", "v1"),
                                                                     sealed("v3", "v2"))))
    assert lines.end_status("v1") == "WITHDRAWN"
    assert lines.end_status("v3") == "WITHDRAWN"
    assert lines.end_status("unknown") is None
```

- [ ] **Step 3: Run the tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/automation/test_authored_lifecycle.py -q --timeout=60`
Expected: FAIL (`AttributeError: 'AuthoredStrategyStore' object has no attribute 'report'`).

- [ ] **Step 4: Add reports, retirement and expiry** (`trader/automation/authored_strategies.py`)

Add to the imports:

```python
import logging

from trader.automation.ai_deployment_activity import EXPIRED as VERSION_EXPIRED
from trader.automation.ai_deployment_activity import JUDGMENT_ENDED, SUPERSEDED, WITHDRAWN
from trader.automation.ai_deployment_versions import WITHDRAWAL_ENTRY_IN_FLIGHT
from trader.automation.ai_deployments import DeploymentRefused
from trader.automation.authored_refusals import AUTHORED_STRATEGY_UNKNOWN
from trader.automation.authored_wire import ReportAuthoredEvaluationRequest, RetireAuthoredStrategyRequest
from trader.automation.backtest_judgments import JudgmentRefused, nth_session_after
from trader.automation.evaluation_claims import ny_day

logger = logging.getLogger(__name__)
```

Add these module functions above `class AuthoredStrategyStore`:

```python
# Ruling 18 and decision D4: a retired record's version is withdrawn through the SP2c path, as the strategy.
AUTHORED_RETIRE_REASON = "AUTHORED_SANDBOX_ERROR"


def authored_retire_command_id(version_digest: str) -> str:
    """One withdrawal per version: a resend reuses the id, and SP2c answers an already withdrawn version False."""
    return f"authored-retire:{version_digest}"


def withdraw_with(versions: Any) -> Callable[[str], bool]:
    """The ``withdraw`` port of AuthoredStrategyStore over SP2c's AiDeploymentVersionStore."""
    def withdraw(version_digest: str) -> bool:
        return versions.withdraw(version_digest, reason=AUTHORED_RETIRE_REASON, principal="strategy",
                                 command_id=authored_retire_command_id(version_digest))
    return withdraw


# Ruling 11: where a DEPLOYED record goes when its version line ends.
_LINE_ENDINGS: Mapping[str, tuple[AuthoredState, str]] = MappingProxyType({
    WITHDRAWN: (RETIRED, OWNER_RETIRED),
    VERSION_EXPIRED: (EXPIRED, DEPLOY_EXPIRED),
    JUDGMENT_ENDED: (EXPIRED, RENEWAL_ENDED),
})


def _already_reported(record: AuthoredRecord, request: ReportAuthoredEvaluationRequest) -> bool:
    """Ruling 17: the same event for the same request (and image) was applied before."""
    return any(entry.get("event") == request.event and entry.get("request_id") == request.request_id
               and entry.get("image") == request.sandbox_image_digest for entry in record.history)


def _apply_report(conn: Any, record: AuthoredRecord, request: ReportAuthoredEvaluationRequest,
                  now: dt.datetime) -> AuthoredRecord:
    detail = request.detail or None
    if request.event == "STARTED":
        return note_in_tx(conn, record, event="STARTED", now=now, request_id=request.request_id, detail=detail,
                          image=request.sandbox_image_digest, sandbox_image_digest=request.sandbox_image_digest,
                          sandbox_image_seen_at=utc(now))
    if request.event == "SANDBOX_ERROR":
        return move_in_tx(conn, record, REJECTED, event="SANDBOX_ERROR", reason=SANDBOX_ERROR, now=now,
                          request_id=request.request_id, detail=detail, evaluation_request_id=None)
    attempts = record.attempts + 1
    target, reason = (FAILED, SANDBOX_UNAVAILABLE) if attempts >= UNAVAILABLE_ATTEMPTS else (STORED, None)
    return move_in_tx(conn, record, target, event="SANDBOX_UNAVAILABLE", reason=reason, now=now,
                      request_id=request.request_id, detail=detail, attempts=attempts, evaluation_request_id=None)


def _existing_in_tx(conn: Any, digest: str) -> AuthoredRecord:
    record = record_in_tx(conn, digest)
    if record is None:
        raise AuthoredRefused(AUTHORED_STRATEGY_UNKNOWN, f"no authored strategy has digest {digest}")
    return record


class DeploymentLines:
    """Where an authored deployment's version line ends: its last version after renewals (ruling 11)."""

    def __init__(self, *, activity: Any, versions: Any):
        self._activity = activity
        self._versions = versions

    def end_status(self, version_digest: str) -> Optional[str]:
        statuses = self._activity.statuses()
        renewed_by = {sealed.version.prior_version: sealed.digest for sealed in self._versions.sealed()
                      if sealed.version.prior_version is not None}
        current, seen = version_digest, set()
        while statuses.get(current) == SUPERSEDED and current in renewed_by and current not in seen:
            seen.add(current)
            current = renewed_by[current]
        return statuses.get(current)
```

Add these methods to `AuthoredStrategyStore`:

```python
    def report(self, request: ReportAuthoredEvaluationRequest) -> dict:
        """Research's facts about one evaluation (spec 7.6, 8; ruling 17). The switch is not checked: an
        evaluation already claimed must finish (ruling 8)."""
        now = utc(self._now())

        def write(conn: Any) -> tuple[str, AuthoredRecord, int]:
            record = _existing_in_tx(conn, request.digest)
            if _already_reported(record, request):
                return "UNCHANGED", record, judged_count_in_tx(conn)
            if record.state != EVALUATING or record.evaluation_request_id != request.request_id:
                raise AuthoredRefused(AUTHORED_STATE_CONFLICT,
                                      f"the record is {record.state.value} on {record.evaluation_request_id}; "
                                      f"the report names {request.request_id}")
            return "UPDATED", _apply_report(conn, record, request, now), judged_count_in_tx(conn)
        try:
            status, record, judged = self._db.transaction(write)
        except AuthoredRefused as refused:
            return refused.reply(judged_count=None)
        return _reply(status, record, judged_count=judged)

    def retire(self, request: RetireAuthoredStrategyRequest) -> dict:
        """The strategy service retires a DEPLOYED record after a live fault (spec 7.6, 8; ruling 18).

        The record moves first, so entries are refused at once (ruling 16) and the reason is SANDBOX_ERROR even
        if the trader dies before the next step. Then the version is withdrawn through the SP2c path (D4), so
        the line ends WITHDRAWN and a restart never lists it. A withdrawal refused while an entry is being sent
        is a retryable refusal; the strategy service resends until UPDATED or UNCHANGED (Plan 4 ruling 18)."""
        now = utc(self._now())

        def write(conn: Any) -> tuple[str, AuthoredRecord]:
            record = _existing_in_tx(conn, request.digest)
            if record.state == RETIRED:
                return "UNCHANGED", record
            if record.state != DEPLOYED:
                raise AuthoredRefused(AUTHORED_STATE_CONFLICT,
                                      f"only a DEPLOYED authored strategy retires; this one is {record.state.value}")
            return "UPDATED", move_in_tx(conn, record, RETIRED, event="RETIRED_BY_STRATEGY", reason=SANDBOX_ERROR,
                                         now=now, detail=f"{request.code}: {request.detail}")
        try:
            status, record = self._db.transaction(write)
        except AuthoredRefused as refused:
            return refused.reply()
        if record.deployment_version is not None and self._withdraw is not None:
            try:
                self._withdraw(record.deployment_version)
            except DeploymentRefused as refused:
                logger.error("authored strategy %s is RETIRED; withdrawing %s refused: %s", record.digest,
                             record.deployment_version, refused)
                return {**AuthoredRefused(refused.code, f"the record is RETIRED; its version is not withdrawn yet: "
                                                        f"{refused.message}",
                                          retryable=refused.code == WITHDRAWAL_ENTRY_IN_FLIGHT).reply(),
                        "record": record.to_json()}
        return _reply(status, record)

    def refresh(self) -> None:
        """Ruling 11: a SHADOW or DEPLOYED record leaves the count when its window ends. Other stores are read
        first; each record moves only if it did not move meanwhile."""
        now = utc(self._now())
        records = self._db.transaction(records_in_tx)
        endings = [(record, ending) for record in records if (ending := self._ending(record, now)) is not None]
        if not endings:
            return

        def write(conn: Any) -> None:
            for record, (target, reason) in endings:
                current = record_in_tx(conn, record.digest)
                if current is not None and current.state_changed_at == record.state_changed_at \
                        and current.state == record.state:
                    move_in_tx(conn, current, target, event="WINDOW_ENDED", reason=reason, now=now)
        self._db.transaction(write)

    def _ending(self, record: AuthoredRecord, now: dt.datetime) -> Optional[tuple[AuthoredState, str]]:
        if record.state == SHADOW:
            return self._shadow_ending(record, now)
        if record.state == DEPLOYED and record.deployment_version is not None and self._lines is not None:
            return _LINE_ENDINGS.get(self._lines().end_status(record.deployment_version))
        return None

    def _shadow_ending(self, record: AuthoredRecord, now: dt.datetime) -> Optional[tuple[AuthoredState, str]]:
        if self._calendar is None:
            return None
        try:
            last = nth_session_after(self._calendar, ny_day(record.state_changed_at), self._shadow_sessions)
        except JudgmentRefused as refused:          # the calendar cannot serve: stay counted, the safe side
            logger.error("authored strategy %s stays SHADOW: %s", record.digest, refused.detail)
            return None
        return (EXPIRED, SHADOW_EXPIRED) if ny_day(now) > last else None
```

In `submit`, call `self.refresh()` right after `self._require_enabled()`:

```python
        try:
            self._require_enabled()
            self.refresh()
            self._require_room()
```

In `overview`, make `self.refresh()` the first line of the body.

- [ ] **Step 5: Run the tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/automation/test_authored_lifecycle.py tests/automation/test_authored_submission.py tests/automation/test_authored_records.py -q --timeout=60`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add trader/automation/authored_strategies.py tests/automation/authored_fixtures.py \
  tests/automation/test_authored_lifecycle.py
git commit -m "feat: move authored records on research reports, retirement and window ends

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---
### Task 7: Authored keys in SP2c claims and judgments

**Files:**
- Modify: `trader/automation/evaluation_claims.py`, `trader/automation/backtest_judgments.py`, `trader/automation/authored_strategies.py` (add `claim_in_tx`, `after_judgment_in_tx`)
- Test: `tests/automation/test_authored_sp2c_hooks.py`

**Interfaces:**
- Consumes: `EvaluationClaims`, `ClaimRefused`, `STRATEGY_NOT_ALLOWED`; `BacktestJudgments`, `BacktestJudgment`, `JudgmentRefused`; Plan 1 `is_authored_key`, `parse_authored_key`; `bar_size_key`; SP2c fixtures `World`, `finished`, `judgment`, `request_body`, `case_keys`, `judge_config`, `count_judgments`.
- Produces:
  - `EvaluationClaims(db, *, config, now, authored=None)`: an authored key passes only through `authored.claim_in_tx(conn, request_id, body, now)`, the last check before the claim row.
  - `BacktestJudgments(..., renewals=None, authored=None)`: after the insert of a judgment of an authored key it calls `authored.after_judgment_in_tx(conn, judgment, recorded_at)` in the same transaction.
  - `AuthoredStrategyStore.claim_in_tx(conn, request_id, body, now) -> None` (raises `ClaimRefused`), `.after_judgment_in_tx(conn, judgment, now) -> None` (raises `JudgmentRefused`), `VERDICT_STATES`.

- [ ] **Step 1: Write the failing tests** (`tests/automation/test_authored_sp2c_hooks.py`)

```python
"""SP3 Plan 3 Task 7: authored keys in SP2c claims and judgments (spec 7.3, 7.7, 8; rulings 7-9)."""
from __future__ import annotations

import pytest

from tests.automation.authored_fixtures import authored_world, report, stored, with_config
from tests.automation.backtest_judge_fixtures import (
    World, case_keys, count_judgments, finished, judge_config, judgment, request_body,
)
from trader.authored.keys import authored_key
from trader.automation.authored_strategies import EVALUATING, STORED
from trader.automation.backtest_judgments import BacktestJudgments
from trader.automation.calendar_policy import XNYSCalendarPolicy
from trader.automation.evaluation_claims import ClaimRefused, EvaluationClaims
from trader.research.evaluation_request import evaluation_request_id


def hooked(tmp_path, *, judge=None, **config):
    """SP2c claims and judgments wired to an authored store on one journal, as the trader wires them."""
    aw = authored_world(tmp_path, **config)
    keys, cfg = case_keys(tmp_path), judge_config(**(judge or {}))
    judgments = BacktestJudgments(aw.db, config=cfg, calendar=XNYSCalendarPolicy(), cases_dir=keys.cases_dir,
                                  verify_dir=keys.verify_dir, now=aw.clock, authored=aw.store)
    claims = EvaluationClaims(aw.db, config=cfg, now=aw.clock, authored=aw.store)
    return World(aw.db, aw.clock, claims, judgments, keys), aw


def authored_body(digest, class_name="RangeBreak", **changes):
    raw = {"strategy_key": authored_key(digest, class_name), "cohort": [{"RANGE_BARS": 10}, {"RANGE_BARS": 20}]}
    raw.update(changes)
    return request_body(**raw)


def claim(w, body):
    return w.claims.claim(evaluation_request_id(body), body, principal="research")


def refused_claim(w, body) -> ClaimRefused:
    with pytest.raises(ClaimRefused) as refused:
        claim(w, body)
    return refused.value


def test_an_accepted_claim_moves_the_record_to_evaluating(tmp_path):
    w, aw = hooked(tmp_path)
    digest = stored(aw)
    body = authored_body(digest)
    assert claim(w, body).status == "ACCEPTED"
    record = aw.store.get(digest)
    assert (record.state, record.evaluation_request_id) == (EVALUATING, evaluation_request_id(body))
    assert claim(w, body).status == "EXISTING"                       # a retry moves nothing again
    assert len(aw.store.get(digest).history) == 2


def test_the_switch_off_refuses_an_authored_claim(tmp_path):                                 # ruling 8
    w, aw = hooked(tmp_path)
    digest = stored(aw)
    claims = EvaluationClaims(aw.db, config=judge_config(), now=aw.clock, authored=with_config(aw, enabled=False))
    with pytest.raises(ClaimRefused) as refused:
        claims.claim(evaluation_request_id(authored_body(digest)), authored_body(digest), principal="research")
    assert refused.value.code == "AUTHORED_STRATEGIES_DISABLED"
    assert aw.store.get(digest).state == STORED


@pytest.mark.parametrize("change, hint", [
    ({"cohort": [{"RANGE_BARS": 40}]}, "outside [5, 30]"),
    ({"cohort": [{"WINDOW": 3}]}, "not a tunable"),
    ({"cohort": [{"RANGE_BARS": "10"}]}, "not a number"),
    ({"bar_size": "1 min"}, "runs on 5 mins"),
])
def test_a_claim_outside_the_authored_file_is_not_allowed(tmp_path, change, hint):           # ruling 7
    w, aw = hooked(tmp_path)
    digest = stored(aw)
    refused = refused_claim(w, authored_body(digest, **change))
    assert refused.code == "STRATEGY_NOT_ALLOWED" and hint in refused.detail
    assert aw.store.get(digest).state == STORED


def test_another_class_an_unknown_digest_or_a_second_claim_is_not_allowed(tmp_path):
    w, aw = hooked(tmp_path)
    digest = stored(aw)
    assert "class" in refused_claim(w, authored_body(digest, class_name="Other")).detail
    assert "no authored strategy" in refused_claim(w, authored_body("sha256:" + "f" * 64)).detail
    claim(w, authored_body(digest))
    second = refused_claim(w, authored_body(digest, research_day="2026-10-07"))
    assert second.code == "STRATEGY_NOT_ALLOWED" and "only a STORED" in second.detail


def test_a_claim_refused_by_the_daily_limit_leaves_the_record_stored(tmp_path):              # review focus 5
    w, aw = hooked(tmp_path, judge={"evaluations_per_day": 1})
    digest = stored(aw)
    assert claim(w, request_body()).status == "ACCEPTED"             # a catalogue claim takes the only slot
    assert refused_claim(w, authored_body(digest)).code == "EVALUATION_LIMIT_REACHED"
    record = aw.store.get(digest)
    assert (record.state, len(record.history)) == (STORED, 1)


def test_an_authored_key_without_a_store_is_not_allowed(tmp_path):
    w, aw = hooked(tmp_path)
    digest = stored(aw)
    claims = EvaluationClaims(aw.db, config=judge_config(), now=aw.clock)
    with pytest.raises(ClaimRefused) as refused:
        claims.claim(evaluation_request_id(authored_body(digest)), authored_body(digest), principal="research")
    assert refused.value.code == "STRATEGY_NOT_ALLOWED"


def judged_case(tmp_path):
    w, aw = hooked(tmp_path)
    digest = stored(aw)
    body = authored_body(digest)
    return w, aw, digest, body, finished(w, body, strategy_file_hash=digest)


@pytest.mark.parametrize("verdict, state, reason", [
    ("REJECT", "REJECTED", "JEV_REJECT"),
    ("SHADOW", "SHADOW", "JEV_SHADOW"),
    ("DEPLOY", "SHADOW", "AWAITING_REGISTRATION"),
    ("NO_VERDICT", "FAILED", "JEV_NO_VERDICT"),
])
def test_a_judgment_moves_the_record_in_its_own_transaction(tmp_path, verdict, state, reason):   # ruling 9
    w, aw, digest, _body, case = judged_case(tmp_path)
    assert w.judgments.record(judgment(case, verdict))["status"] == "RECORDED"
    record = aw.store.get(digest)
    assert (record.state.value, record.reason, record.verdict, record.judgment_id) == (
        state, reason, verdict, "jdg-00000001")
    assert record.evaluation_request_id is None
    assert aw.store.overview()["judged_count"] == 1                 # spec 7.7: Plan 4 adds it to the next case


def test_a_judgment_for_a_record_not_evaluating_on_that_request_records_nothing(tmp_path):    # review focus 5
    w, aw, digest, body, case = judged_case(tmp_path)
    report(aw, digest, evaluation_request_id(body), "SANDBOX_ERROR")
    receipt = w.judgments.record(judgment(case, "SHADOW"))
    assert (receipt["status"], receipt["code"]) == ("REFUSED", "AUTHORED_STATE_CONFLICT")
    assert count_judgments(w.db) == 0
    assert aw.store.get(digest).state.value == "REJECTED"


def test_an_authored_case_without_a_store_is_refused(tmp_path):
    w, aw, digest, _body, case = judged_case(tmp_path)
    bare = BacktestJudgments(aw.db, config=judge_config(), calendar=XNYSCalendarPolicy(),
                             cases_dir=w.keys.cases_dir, verify_dir=w.keys.verify_dir, now=aw.clock)
    receipt = bare.record(judgment(case, "SHADOW"))
    assert (receipt["status"], receipt["code"]) == ("REFUSED", "AUTHORED_STRATEGIES_DISABLED")
    assert count_judgments(w.db) == 0
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/automation/test_authored_sp2c_hooks.py -q --timeout=60`
Expected: FAIL (`TypeError: EvaluationClaims.__init__() got an unexpected keyword argument 'authored'`).

- [ ] **Step 3: Let claims ask the store** (`trader/automation/evaluation_claims.py`)

Add `from trader.authored.keys import is_authored_key` to the imports. Replace `__init__`, the part of `claim` from `self._check_config(body)` to the INSERT, and `_check_config` with:

```python
    def __init__(self, db: Any, *, config: BacktestJudgeConfig, now: Callable[[], dt.datetime],
                 authored: Any = None):
        """``authored`` is the trader's AuthoredStrategyStore (SP3); without it an authored key is never claimable."""
        self._db = db
        self._config = config
        self._now = now
        self._authored = authored
```

```python
            self._check_config(body)
            until = cooling_until_in_tx(conn, body.strategy_key, day)
            if until is not None:
                raise ClaimRefused(FAMILY_COOLING_DOWN,
                                   f"{body.strategy_key} cools down until session {until.isoformat()}")
            used = conn.execute("SELECT COUNT(*) FROM evaluation_claims WHERE ny_day = ?", [day]).fetchone()[0]
            if used >= self._config.evaluations_per_day:
                raise ClaimRefused(EVALUATION_LIMIT_REACHED, f"{used} of {self._config.evaluations_per_day} "
                                                             f"evaluations already claimed on {day.isoformat()}")
            if is_authored_key(body.strategy_key):
                # SP3 Plan 3 ruling 7: the last check, because it moves the record; any refusal rolls it back.
                self._authored.claim_in_tx(conn, request_id, body, now)
            conn.execute(f"INSERT INTO evaluation_claims ({_COLUMNS}) VALUES (?, ?, ?, ?, 'QUEUED', ?, ?, ?)",
                         [request_id, canonical, body.strategy_key, day, principal, now, now])
```

```python
    def _check_config(self, body: EvaluationRequestBody) -> None:
        if is_authored_key(body.strategy_key):
            if self._authored is None:
                raise ClaimRefused(STRATEGY_NOT_ALLOWED, "this trader has no authored-strategy store")
        elif not self._config.allows(body.strategy_key):
            raise ClaimRefused(STRATEGY_NOT_ALLOWED,
                               f"{body.strategy_key} is not on ai_paper.backtest_judge.strategy_allowlist")
        if len(body.cohort) > self._config.max_cohort_points:
            raise ClaimRefused(COHORT_TOO_LARGE,
                               f"{len(body.cohort)} points; the limit is {self._config.max_cohort_points}")
```

- [ ] **Step 4: Let a judgment move its authored record** (`trader/automation/backtest_judgments.py`)

Add `from trader.authored.keys import is_authored_key` to the imports. Change the constructor signature and body end to:

```python
    def __init__(self, db: Any, *, config: BacktestJudgeConfig, calendar: Any, cases_dir: Path, verify_dir: Path,
                 now: Callable[[], dt.datetime], renewals: Optional[RenewalChecks] = None, authored: Any = None):
        """``authored`` is the AuthoredStrategyStore (SP3 Plan 3 ruling 9); without it an authored case is refused."""
        self._db = db
        self._config = config
        self._calendar = calendar
        self._cases_dir = Path(cases_dir)
        self._verify_dir = Path(verify_dir)
        self._now = now
        self._renewals = renewals if renewals is not None else NoRenewalVersions()
        self._authored = authored
```

In `_insert_in_tx`, between the `conn.execute(f"INSERT INTO backtest_judgments ...")` call and the final `return _receipt("RECORDED", ...)`, add:

```python
        if is_authored_key(case.strategy_key):
            self._authored_judged_in_tx(conn, judgment, recorded_at)
```

and add the method:

```python
    def _authored_judged_in_tx(self, conn: Any, judgment: BacktestJudgment, recorded_at: dt.datetime) -> None:
        """Same transaction as the judgment row: a refusal here records nothing (SP3 Plan 3 ruling 9)."""
        if self._authored is None:
            raise JudgmentRefused("AUTHORED_STRATEGIES_DISABLED", "this trader has no authored-strategy store")
        self._authored.after_judgment_in_tx(conn, judgment, recorded_at)
```

- [ ] **Step 5: Add the two hooks to the store** (`trader/automation/authored_strategies.py`)

Add to the imports:

```python
from trader.authored.keys import parse_authored_key
from trader.automation.evaluation_claims import STRATEGY_NOT_ALLOWED, ClaimRefused
from trader.automation.strategy_binding import bar_size_key
from trader.research.evaluation_request import EvaluationRequestBody
```

Add above `class AuthoredStrategyStore`:

```python
# Ruling 9: an INITIAL judgment's verdict and the state it gives the record.
VERDICT_STATES: Mapping[str, tuple[AuthoredState, str]] = MappingProxyType({
    "DEPLOY": (SHADOW, AWAITING_REGISTRATION),
    "SHADOW": (SHADOW, JEV_SHADOW),
    "REJECT": (REJECTED, JEV_REJECT),
    "NO_VERDICT": (FAILED, JEV_NO_VERDICT),
})


def _claim_problem(record: Optional[AuthoredRecord], class_name: str, body: EvaluationRequestBody) -> Optional[str]:
    """Ruling 7: why ``body`` may not evaluate ``record``, or None."""
    if record is None:
        return "no authored strategy has this digest"
    if record.state != STORED:
        return f"the authored strategy is {record.state.value}; only a STORED one may be claimed"
    if record.class_name != class_name:
        return f"the authored file's class is {record.class_name}, not {class_name}"
    if bar_size_key(body.bar_size) != bar_size_key(record.bar_size):
        return f"the authored file runs on {record.bar_size}, not {body.bar_size}"
    for point in body.cohort:
        for name, value in point.items():
            problem = _point_problem(record.tunables, name, value)
            if problem is not None:
                return problem
    return None


def _point_problem(tunables: Mapping[str, Mapping[str, float]], name: str, value: Any) -> Optional[str]:
    bounds = tunables.get(name)
    if bounds is None:
        return f"{name} is not a tunable of the authored file"
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return f"{name}={value!r} is not a number"
    if not bounds["low"] <= value <= bounds["high"]:
        return f"{name}={value!r} is outside [{bounds['low']}, {bounds['high']}]"
    return None
```

Add these methods to `AuthoredStrategyStore`:

```python
    def claim_in_tx(self, conn: Any, request_id: str, body: EvaluationRequestBody, now: dt.datetime) -> None:
        """Ruling 7, inside the SP2c claim transaction: STORED -> EVALUATING, or ClaimRefused."""
        if not self._config.enabled:
            raise ClaimRefused(AUTHORED_STRATEGIES_DISABLED, "ai_paper.authored_strategies.enabled is false")
        digest, class_name = parse_authored_key(body.strategy_key)
        record = record_in_tx(conn, digest)
        problem = _claim_problem(record, class_name, body)
        if problem is not None:
            raise ClaimRefused(STRATEGY_NOT_ALLOWED, problem)
        move_in_tx(conn, record, EVALUATING, event="CLAIMED", reason=None, now=now, request_id=request_id,
                   evaluation_request_id=request_id)

    def after_judgment_in_tx(self, conn: Any, judgment: Any, now: dt.datetime) -> None:
        """Ruling 9, inside the judgment's transaction. A renewal ends a line; ``refresh`` reads that from the
        deployment versions, so only an INITIAL judgment moves the record here."""
        if judgment.kind != "INITIAL":
            return
        digest, class_name = parse_authored_key(judgment.strategy_key)
        record = record_in_tx(conn, digest)
        if record is None or record.state != EVALUATING or record.class_name != class_name \
                or record.evaluation_request_id != judgment.request_id:
            found = "missing" if record is None else f"{record.state.value} on {record.evaluation_request_id}"
            raise JudgmentRefused(AUTHORED_STATE_CONFLICT,
                                  f"the authored record is {found}; the judgment names {judgment.request_id}")
        target, reason = VERDICT_STATES[judgment.verdict]
        move_in_tx(conn, record, target, event="JUDGED", reason=reason, now=now, request_id=judgment.request_id,
                   verdict=judgment.verdict, judgment_id=judgment.judgment_id, evaluation_request_id=None)
```

- [ ] **Step 6: Run the tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/automation/test_authored_sp2c_hooks.py tests/automation/test_evaluation_claims.py tests/automation/test_backtest_judgments.py tests/automation/test_backtest_judge_surface.py tests/automation/test_renewal_judgments.py tests/automation/test_authored_lifecycle.py -q --timeout=60`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add trader/automation/evaluation_claims.py trader/automation/backtest_judgments.py \
  trader/automation/authored_strategies.py tests/automation/test_authored_sp2c_hooks.py
git commit -m "feat: let sp2c claims and judgments move authored records

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---
### Task 8: Isolation proofs, the staged-deploy gate and the entry gate

**Files:**
- Create: `trader/automation/authored_isolation.py`
- Modify: `trader/automation/ai_deployment_registration.py`, `trader/automation/ai_deployment_activity.py`, `trader/automation/authored_strategies.py` (add `running_image`, `require_proven_in_tx`, `registration_refusal`, `mark_deployed_in_tx`, `entry_refusal`, `send_gate_in_tx`), `tests/automation/authored_fixtures.py`, `tests/automation/ai_paper_world.py` (`arm_authored`)
- Test: `tests/automation/test_authored_isolation_gate.py`, `tests/automation/test_authored_send_gate.py`

**Interfaces:**
- Consumes: `RecordIsolationProofRequest`; `AiDeploymentRegistrar`, `DeploymentActivity`, `deployment_version_gate`, `DeploymentRefused`, `AiDeployment`; `is_authored_path`, `authored_digest_of_path`, `authored_strategy_path`; `AI_PAPER_ACTION` (`trader/automation/ai_paper_evidence.py`); test helpers `SeededJudgments`, `Cooldowns`, `deploy_facts` (`tests/automation/judged_deployment.py`) and `FakeBundles` (`tests/automation/test_ai_deployment_registration.py`).
- Produces:
  - `IsolationProof` (`.to_json()`), `IsolationProofs(db, *, now)` with `.record(request, *, principal) -> dict`, `.latest(image_digest)`, `.latest_in_tx(conn, image_digest)`, `.passing_for(image_digest) -> bool`, `.passing_for_in_tx(conn, image_digest) -> bool`; `PROOF_DOMAIN`.
  - `AuthoredStrategyStore.running_image() -> str | None`, `.require_proven_in_tx(conn, deployment) -> None` (both the build that evaluated the record and the build research reported last must have a passing proof; raises `DeploymentRefused`), `.registration_refusal(deployment, judgment_id, *, kind) -> None` (raises `DeploymentRefused`), `.mark_deployed_in_tx(conn, deployment, judgment_id, version_digest, now) -> None`, `.entry_refusal(deployment) -> str | None`, `.send_gate_in_tx(conn, request) -> str | None` (the SUBMITTING-transaction check, review r1 #4).
  - `AiDeploymentRegistrar(..., authored=None)` and `DeploymentActivity(..., authored=None)`; `DeploymentActivity.active()` leaves out an authored deployment that `entry_refusal` would refuse.
  - Fixture `proof_request(image=IMAGE, *, passed=True, ran_at=NOW) -> RecordIsolationProofRequest`.

- [ ] **Step 1: Add the proof fixture** (append to `tests/automation/authored_fixtures.py`)

Add the import `from trader.automation.authored_wire import RecordIsolationProofRequest` and append:

```python
def proof_request(image: str = IMAGE, *, passed: bool = True, ran_at=NOW) -> RecordIsolationProofRequest:
    return RecordIsolationProofRequest.model_validate({
        "image_digest": image, "passed": passed, "ran_at": ran_at.isoformat(),
        "checks": [{"name": "hostile.os_kill_parent", "passed": passed, "detail": "EPERM"},
                   {"name": "host.network_none", "passed": True, "detail": "NetworkMode=none"}]})
```

- [ ] **Step 2: Write the failing tests** (`tests/automation/test_authored_isolation_gate.py`)

```python
"""SP3 Plan 3 Task 8: isolation proofs, staged deploy and the entry gate (spec 7.9, 8, 10 Staged deploy and
Authority; rulings 10, 12, 16, 21)."""
from __future__ import annotations

import datetime as dt
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from tests.automation.authored_fixtures import (
    IMAGE, OTHER_IMAGE, authored_journal, digest_of, proof_request, request_id, source, submit_request,
)
from tests.automation.backtest_judge_fixtures import Clock
from tests.automation.judged_deployment import Cooldowns, SeededJudgments, deploy_facts
from tests.automation.test_ai_deployment_registration import FakeBundles
from trader.automation.ai_bundle_check import BundleFacts
from trader.automation.ai_deployment_activity import DeploymentActivity, deployment_version_gate
from trader.automation.ai_deployment_registration import AiDeploymentRegistrar, registration_command_id
from trader.automation.ai_deployment_versions import AiDeploymentVersionStore, apply_ai_deployment_version_migrations
from trader.automation.ai_deployments import (
    AiDeployment, AiDeploymentStore, DeploymentRefused, apply_ai_deployment_migration,
)
from trader.automation.ai_paper_decision import apply_ai_paper_decision_migration
from trader.automation.ai_paper_evidence import AI_PAPER_ACTION
from trader.automation.authored_config import AuthoredStrategiesConfig
from trader.automation.authored_files import AuthoredFileStore
from trader.automation.authored_isolation import IsolationProofs
from trader.automation.authored_strategies import (
    DEPLOYED, EVALUATING, SHADOW, AuthoredStrategyStore, move_in_tx, record_in_tx,
)
from trader.automation.authored_wire import ReportAuthoredEvaluationRequest, RetireAuthoredStrategyRequest
from trader.automation.calendar_policy import XNYSCalendarPolicy
from trader.automation.protective_order_saga import apply_protective_order_saga_migration
from trader.data.domain_journal import DomainJournal
from trader.data.schema_migrations import SchemaMigrator
from trader.research.strategy_key import authored_strategy_path

UTC = dt.timezone.utc
NOW = dt.datetime(2026, 10, 9, 21, 0, tzinfo=UTC)         # Friday after the close
FIRST_SESSION = dt.datetime(2026, 10, 12, 15, 0, tzinfo=UTC)


# -- proofs ------------------------------------------------------------------------------------------------

def test_a_proof_is_recorded_once_and_read_back(tmp_path):
    proofs = IsolationProofs(authored_journal(tmp_path), now=Clock(NOW))
    first = proofs.record(proof_request(), principal="cli")
    assert (first["status"], first["proof"]["image_digest"], first["proof"]["passed"]) == ("RECORDED", IMAGE, True)
    assert proofs.record(proof_request(), principal="cli") == {"status": "EXISTING", "proof": first["proof"]}
    assert proofs.latest(IMAGE).to_json() == first["proof"]
    assert proofs.latest(None).image_digest == IMAGE and proofs.passing_for(IMAGE)


def test_a_later_failing_run_of_the_same_image_withdraws_the_proof(tmp_path):
    clock = Clock(NOW)
    proofs = IsolationProofs(authored_journal(tmp_path), now=clock)
    proofs.record(proof_request(), principal="cli")
    clock.now = NOW + dt.timedelta(hours=1)
    proofs.record(proof_request(passed=False, ran_at=clock.now), principal="cli")
    assert not proofs.passing_for(IMAGE)


def test_each_image_needs_its_own_run(tmp_path):                                   # spec 10 Staged deploy
    proofs = IsolationProofs(authored_journal(tmp_path), now=Clock(NOW))
    proofs.record(proof_request(IMAGE), principal="cli")
    assert proofs.passing_for(IMAGE) and not proofs.passing_for(OTHER_IMAGE) and not proofs.passing_for(None)


@pytest.mark.parametrize("change", [
    {"passed": True, "checks": [{"name": "a", "passed": False, "detail": ""}]},      # ruling 21
    {"checks": [{"name": "a", "passed": True, "detail": ""}, {"name": "a", "passed": True, "detail": ""}]},
    {"checks": []},
    {"ran_at": "2026-10-09T21:00:00"},                                                # no offset
    {"image_digest": "unset"},                                                         # Plan 2: never started
    {"image_digest": "has space"},
])
def test_an_inconsistent_proof_body_never_passes_the_wire(change):
    raw = {**proof_request().model_dump(), **change}
    with pytest.raises(ValidationError):
        type(proof_request()).model_validate(raw)


# -- registration and entries ------------------------------------------------------------------------------

class Stack:
    """SP2c registration and activity over one journal, wired to an authored store as the trader wires them."""

    def __init__(self, tmp_path):
        self.tmp_path, self.now = tmp_path, NOW
        self.db = authored_journal(tmp_path)
        migrator = SchemaMigrator(self.db)
        for apply in (apply_ai_deployment_migration, apply_protective_order_saga_migration,
                      apply_ai_paper_decision_migration, apply_ai_deployment_version_migrations):
            apply(migrator)
        self.journal = DomainJournal(self.db)
        self.journal.migrate(migrator)
        clock = lambda: self.now                                                   # noqa: E731
        self.proofs = IsolationProofs(self.db, now=clock)
        self.deployments = AiDeploymentStore(self.db, now=clock)
        self.versions = AiDeploymentVersionStore(self.journal, now=clock)
        self.judgments, self.cooldowns, self.bundles = SeededJudgments(), Cooldowns(), FakeBundles()
        self.rebuild(enabled=True)

    def rebuild(self, *, enabled: bool) -> None:
        """A trader restart with the switch set to ``enabled``."""
        clock = lambda: self.now                                                   # noqa: E731
        self.store = AuthoredStrategyStore(self.db, config=AuthoredStrategiesConfig(enabled=enabled),
                                           files=AuthoredFileStore(self.tmp_path / "store"), now=clock,
                                           proofs=self.proofs)
        self.activity = DeploymentActivity(versions=self.versions, deployments=self.deployments,
                                           judgments=self.judgments, cooldowns=self.cooldowns, max_active=3,
                                           now=clock, authored=self.store)
        self.registrar = AiDeploymentRegistrar(
            journal=self.journal, deployments=self.deployments, versions=self.versions, activity=self.activity,
            judgments=self.judgments, cooldowns=self.cooldowns, bundles=self.bundles,
            calendar=XNYSCalendarPolicy(), expiry_sessions=20, now=clock, authored=self.store)

    def awaiting(self, *, range_bars: int = 15, judgment_id: str = "jdg-00000001", image: str = IMAGE):
        """Stored, claimed, STARTED on ``image`` and judged DEPLOY: SHADOW AWAITING_REGISTRATION."""
        digest = self.store.submit(submit_request(source(range_bars)))["record"]["digest"]
        claim = request_id(range_bars)
        self.db.transaction(lambda conn: move_in_tx(
            conn, record_in_tx(conn, digest), EVALUATING, event="CLAIMED", reason=None, now=self.now,
            request_id=claim, evaluation_request_id=claim))
        self.store.report(ReportAuthoredEvaluationRequest.model_validate({
            "digest": digest, "request_id": claim, "event": "STARTED", "sandbox_image_digest": image,
            "detail": ""}))
        self.db.transaction(lambda conn: move_in_tx(
            conn, record_in_tx(conn, digest), SHADOW, event="JUDGED", reason="AWAITING_REGISTRATION", now=self.now,
            verdict="DEPLOY", judgment_id=judgment_id, evaluation_request_id=None))
        record = self.deployment_record(digest, range_bars)
        self.judge(judgment_id, record)
        return digest, record

    @staticmethod
    def deployment_record(digest: str, n: int) -> dict:
        return {"strategy_path": authored_strategy_path(digest), "strategy_digest": digest,
                "class_name": "RangeBreak", "params": {"RANGE_BARS": 10}, "conids": [265598, 272093],
                "bar_size": "5 mins", "style": "intraday_long", "decider": "jev", "decider_verdict": "DEPLOY",
                "evidence_ref": f"sha256:{n:064x}", "evidence_order_notional": 2000.0}

    def judge(self, judgment_id: str, record: dict) -> None:
        self.judgments.seed(deploy_facts(record, judgment_id))
        self.bundles.facts[record["evidence_ref"]] = BundleFacts(
            record["evidence_ref"], "art-1", "fam-1", record["strategy_path"], record["class_name"],
            record["strategy_digest"], record["params"], tuple(sorted(record["conids"])), record["bar_size"],
            record["evidence_order_notional"], f"jev-model#{judgment_id}", "llm", NOW + dt.timedelta(days=90))

    def register(self, judgment_id: str, record: dict) -> dict:
        body = {"judgment_id": judgment_id, "bundle_digest": record["evidence_ref"],
                "deployment": AiDeployment.from_json(record).to_json()}
        return self.registrar.register(body, principal="ai_research",
                                       command_id=registration_command_id(body, self.now.date()))

    def refused(self, judgment_id: str, record: dict) -> str:
        with pytest.raises(DeploymentRefused) as refused:
            self.register(judgment_id, record)
        return refused.value.code


def test_staged_deploy_registers_nothing_until_the_running_image_has_a_passing_proof(tmp_path):  # spec 10
    s = Stack(tmp_path)
    digest, record = s.awaiting()
    assert s.store.running_image() == IMAGE
    assert s.refused("jdg-00000001", record) == "AUTHORED_ISOLATION_UNPROVEN"
    assert s.versions.sealed() == () and s.store.get(digest).state == SHADOW
    s.proofs.record(proof_request(OTHER_IMAGE), principal="cli")
    assert s.refused("jdg-00000001", record) == "AUTHORED_ISOLATION_UNPROVEN"
    s.proofs.record(proof_request(IMAGE), principal="cli")
    outcome = s.register("jdg-00000001", record)
    assert outcome["created"] is True
    stored = s.store.get(digest)
    assert (stored.state, stored.deployment_version) == (DEPLOYED, outcome["version_digest"])


def test_a_rebuilt_image_needs_a_new_run(tmp_path):                                           # spec 7.9
    s = Stack(tmp_path)
    s.proofs.record(proof_request(IMAGE), principal="cli")
    _first, first_record = s.awaiting(range_bars=15, judgment_id="jdg-00000001", image=IMAGE)
    assert s.register("jdg-00000001", first_record)["created"] is True
    s.now = NOW + dt.timedelta(minutes=5)              # the sandbox was rebuilt; research's next STARTED says so
    _second, second_record = s.awaiting(range_bars=16, judgment_id="jdg-00000002", image=OTHER_IMAGE)
    assert s.store.running_image() == OTHER_IMAGE
    assert s.refused("jdg-00000002", second_record) == "AUTHORED_ISOLATION_UNPROVEN"
    s.proofs.record(proof_request(OTHER_IMAGE), principal="cli")
    assert s.register("jdg-00000002", second_record)["created"] is True


def test_a_failed_run_recorded_after_the_preflight_stops_the_seal(tmp_path):              # review r1 #5
    s = Stack(tmp_path)
    s.proofs.record(proof_request(IMAGE), principal="cli")
    digest, record = s.awaiting()
    preflight = s.store.registration_refusal

    def preflight_then_a_failed_run(*args, **kwargs):
        preflight(*args, **kwargs)                       # the preflight passed on the old proof
        s.now = s.now + dt.timedelta(minutes=1)
        s.proofs.record(proof_request(IMAGE, passed=False, ran_at=s.now), principal="cli")
    s.store.registration_refusal = preflight_then_a_failed_run
    assert s.refused("jdg-00000001", record) == "AUTHORED_ISOLATION_UNPROVEN"
    assert s.versions.sealed() == () and s.store.get(digest).state == SHADOW


def test_the_evaluated_build_and_the_latest_reported_build_must_both_be_proven(tmp_path):  # review r1 #5
    s = Stack(tmp_path)
    s.proofs.record(proof_request(IMAGE), principal="cli")
    _first, first_record = s.awaiting(range_bars=15, judgment_id="jdg-00000001", image=IMAGE)
    s.now = NOW + dt.timedelta(minutes=5)
    s.awaiting(range_bars=16, judgment_id="jdg-00000002", image=OTHER_IMAGE)   # research now sees a rebuilt sandbox
    assert s.store.running_image() == OTHER_IMAGE
    assert s.refused("jdg-00000001", first_record) == "AUTHORED_ISOLATION_UNPROVEN"
    s.proofs.record(proof_request(OTHER_IMAGE), principal="cli")
    assert s.register("jdg-00000001", first_record)["created"] is True


def test_registration_is_refused_while_the_switch_is_off(tmp_path):                           # ruling 8
    s = Stack(tmp_path)
    s.proofs.record(proof_request(IMAGE), principal="cli")
    _digest, record = s.awaiting()
    s.rebuild(enabled=False)
    assert s.refused("jdg-00000001", record) == "AUTHORED_STRATEGIES_DISABLED"


def test_registration_needs_the_record_to_await_that_judgment(tmp_path):
    s = Stack(tmp_path)
    s.proofs.record(proof_request(IMAGE), principal="cli")
    _digest, record = s.awaiting(judgment_id="jdg-00000001")
    s.judge("jdg-00000002", record)                     # another DEPLOY judgment of the same deployment
    assert s.refused("jdg-00000002", record) == "AUTHORED_STATE_CONFLICT"


def test_a_path_naming_another_file_than_the_digest_is_a_mismatch(tmp_path):
    s = Stack(tmp_path)
    s.proofs.record(proof_request(IMAGE), principal="cli")
    digest, record = s.awaiting()
    other = {**record, "strategy_path": authored_strategy_path(digest_of(source(29)))}
    s.judge("jdg-00000001", other)
    assert s.refused("jdg-00000001", other) == "JUDGMENT_MISMATCH"


def registered(s: Stack):
    s.proofs.record(proof_request(IMAGE), principal="cli")
    digest, record = s.awaiting()
    outcome = s.register("jdg-00000001", record)
    s.now = FIRST_SESSION
    return digest, outcome["digest"], outcome["version_digest"]


def test_an_authored_entry_needs_the_switch_and_a_deployed_record(tmp_path):                 # ruling 16
    s = Stack(tmp_path)
    digest, base, version = registered(s)
    assert s.activity.entry_refusal(deployment_digest=base, version_digest=version, source_digest=digest) is None
    assert [active.version_digest for active in s.activity.active()] == [version]
    s.rebuild(enabled=False)
    assert s.activity.entry_refusal(deployment_digest=base, version_digest=version,
                                    source_digest=digest) == "AUTHORED_STRATEGIES_DISABLED"
    assert s.activity.active() == ()                      # the strategy service does not load it either
    s.rebuild(enabled=True)
    s.store.retire(RetireAuthoredStrategyRequest.model_validate(
        {"digest": digest, "code": "SANDBOX_ERROR", "detail": "child crashed"}))
    assert s.activity.entry_refusal(deployment_digest=base, version_digest=version,
                                    source_digest=digest) == "DEPLOYMENT_NOT_ACTIVE"
    assert s.activity.active() == ()                      # a retired strategy is not reloaded after a restart


def test_switch_off_refuses_an_authored_enter_at_dispatch_but_never_a_close(tmp_path):      # spec 10 Authority
    s = Stack(tmp_path)
    digest, base, version = registered(s)
    s.rebuild(enabled=False)
    gate = deployment_version_gate(kind_of=s.deployments.kind_of, activity=s.activity)
    enter = SimpleNamespace(action=AI_PAPER_ACTION, body={
        "action": "ENTER", "deployment_digest": base, "deployment_version": version, "source_digest": digest,
        "decision_id": "dec-1"})
    close = SimpleNamespace(action=AI_PAPER_ACTION, body={"action": "CLOSE", "deployment_digest": None,
                                                           "decision_id": "dec-2"})
    assert gate(enter, None, None, s.now) == "AUTHORED_STRATEGIES_DISABLED"
    assert gate(close, None, None, s.now) is None


def test_an_authored_deployment_without_a_store_never_enters(tmp_path):
    s = Stack(tmp_path)
    digest, base, version = registered(s)
    bare = DeploymentActivity(versions=s.versions, deployments=s.deployments, judgments=s.judgments,
                              cooldowns=s.cooldowns, max_active=3, now=lambda: s.now)
    assert bare.entry_refusal(deployment_digest=base, version_digest=version,
                              source_digest=digest) == "AUTHORED_STRATEGIES_DISABLED"
```

- [ ] **Step 3: Run the tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/automation/test_authored_isolation_gate.py -q --timeout=60`
Expected: FAIL (`ModuleNotFoundError: trader.automation.authored_isolation`).

- [ ] **Step 4: Write the proof store** (`trader/automation/authored_isolation.py`)

```python
"""Isolation proofs of the running sandbox image (SP3 spec 7.9; Plan 3 rulings 12, 21, 22).

``mmr ai-author prove-isolation`` (operator, host) records one row per run: the image digest, pass or
fail, and each check. Rows are insert-only. A rebuilt image has another digest and needs its own run.
Rows hold no secret and no strategy code.
"""
from __future__ import annotations

import datetime as dt
import json
from dataclasses import dataclass
from typing import Any, Callable, Optional

from trader.automation.authored_wire import RecordIsolationProofRequest
from trader.automation.evaluation_claims import utc
from trader.research.canonical import canonical_json_bytes, sha256_digest

PROOF_DOMAIN = "mmr.authored.isolation-proof.v1"
_COLUMNS = "proof_id, image_digest, passed, checks_json, ran_at, recorded_at, principal"


@dataclass(frozen=True)
class IsolationProof:
    proof_id: str
    image_digest: str
    passed: bool
    checks: tuple[dict, ...]
    ran_at: dt.datetime
    recorded_at: dt.datetime
    principal: str

    def to_json(self) -> dict:
        return {"proof_id": self.proof_id, "image_digest": self.image_digest, "passed": self.passed,
                "checks": [dict(check) for check in self.checks], "ran_at": self.ran_at.isoformat(),
                "recorded_at": self.recorded_at.isoformat()}


def _proof(row: tuple) -> IsolationProof:
    return IsolationProof(row[0], row[1], bool(row[2]), tuple(json.loads(row[3])), utc(row[4]), utc(row[5]), row[6])


class IsolationProofs:
    def __init__(self, db: Any, *, now: Callable[[], dt.datetime]):
        self._db = db
        self._now = now

    def record(self, request: RecordIsolationProofRequest, *, principal: str) -> dict:
        body = request.model_dump()
        proof_id = "sha256:" + sha256_digest(PROOF_DOMAIN, body)
        checks = tuple(body["checks"])

        def write(conn: Any) -> dict:
            row = conn.execute(f"SELECT {_COLUMNS} FROM authored_isolation_proofs WHERE proof_id = ?",
                               [proof_id]).fetchone()
            if row is not None:                    # the same run sent twice
                return {"status": "EXISTING", "proof": _proof(row).to_json()}
            proof = IsolationProof(proof_id, request.image_digest, request.passed, checks, request.ran_at_utc(),
                                   utc(self._now()), principal)
            conn.execute(f"INSERT INTO authored_isolation_proofs ({_COLUMNS}) VALUES (?, ?, ?, ?, ?, ?, ?)",
                         [proof.proof_id, proof.image_digest, proof.passed,
                          canonical_json_bytes(list(checks)).decode("utf-8"), proof.ran_at, proof.recorded_at,
                          principal])
            return {"status": "RECORDED", "proof": proof.to_json()}
        return self._db.transaction(write)

    def latest_in_tx(self, conn: Any, image_digest: Optional[str]) -> Optional[IsolationProof]:
        """The newest proof of ``image_digest`` (by recording time), or the newest of all when None."""
        if image_digest is None:
            row = conn.execute(f"SELECT {_COLUMNS} FROM authored_isolation_proofs "
                               "ORDER BY recorded_at DESC, proof_id DESC LIMIT 1").fetchone()
        else:
            row = conn.execute(f"SELECT {_COLUMNS} FROM authored_isolation_proofs WHERE image_digest = ? "
                               "ORDER BY recorded_at DESC, proof_id DESC LIMIT 1", [image_digest]).fetchone()
        return None if row is None else _proof(row)

    def latest(self, image_digest: Optional[str]) -> Optional[IsolationProof]:
        return self._db.transaction(lambda conn: self.latest_in_tx(conn, image_digest))

    def passing_for_in_tx(self, conn: Any, image_digest: Optional[str]) -> bool:
        """True only when the newest run of exactly this image passed: a later failing run withdraws it. Read
        inside the registration's seal transaction too (review r1 #5)."""
        if image_digest is None:
            return False
        latest = self.latest_in_tx(conn, image_digest)
        return latest is not None and latest.passed

    def passing_for(self, image_digest: Optional[str]) -> bool:
        return self._db.transaction(lambda conn: self.passing_for_in_tx(conn, image_digest))
```

- [ ] **Step 5: Add the gates to the store** (`trader/automation/authored_strategies.py`)

Add to the imports:

```python
from trader.automation.ai_deployments import AiDeployment, DeploymentRefused
from trader.automation.authored_refusals import AUTHORED_ISOLATION_UNPROVEN
from trader.research.strategy_key import authored_digest_of_path
```

Add above `class AuthoredStrategyStore`:

```python
def _running_image_in_tx(conn: Any) -> Optional[str]:
    row = conn.execute("SELECT sandbox_image_digest FROM authored_strategies WHERE sandbox_image_seen_at "
                       "IS NOT NULL ORDER BY sandbox_image_seen_at DESC, digest LIMIT 1").fetchone()
    return None if row is None else row[0]


def _may_register(record: AuthoredRecord, judgment_id: str, class_name: str, kind: str) -> bool:
    """Ruling 10: an INITIAL DEPLOY waits in SHADOW for its own judgment; a renewal keeps a DEPLOYED record."""
    if record.class_name != class_name:
        return False
    if kind == "RENEWAL":
        return record.state == DEPLOYED
    return record.state == SHADOW and record.verdict == "DEPLOY" and record.judgment_id == judgment_id
```

Add these methods to `AuthoredStrategyStore`:

```python
    def running_image(self) -> Optional[str]:
        """Ruling 12: the trader cannot reach the sandbox; the image is the one in research's latest STARTED."""
        return self._db.transaction(_running_image_in_tx)

    def require_proven_in_tx(self, conn: Any, deployment: AiDeployment) -> None:
        """Ruling 12 (review r1 #5): the build that evaluated this record (its own STARTED) and the build research
        reported last must both have a passing newest proof. Run before the seal and again inside it, so a
        failed run recorded after the preflight stops the seal. The binding check of the build that will run the
        code live is the strategy service's, at every new live session (Plan 4 ruling 19)."""
        record = record_in_tx(conn, authored_digest_of_path(deployment.strategy_path))
        builds = (None if record is None else record.sandbox_image_digest, _running_image_in_tx(conn))
        unproven = [build or "(none reported)" for build in dict.fromkeys(builds)
                    if self._proofs is None or not self._proofs.passing_for_in_tx(conn, build)]
        if unproven:
            raise DeploymentRefused(AUTHORED_ISOLATION_UNPROVEN,
                                    f"no passing isolation proof for the sandbox build {', '.join(unproven)}; "
                                    "run mmr ai-author prove-isolation")

    def send_gate_in_tx(self, conn: Any, request: Any) -> Optional[str]:
        """Review r1 #4: the last check of an authored ENTER, on the saga's SUBMITTING transaction. A retirement
        or a switch-off commits either before this read (the send is refused) or after the SUBMITTING row
        (the entry was already in flight, as with an SP2c withdrawal)."""
        from trader.automation.ai_paper_evidence import AI_PAPER_ACTION

        body = getattr(request, "body", None) or {}
        if getattr(request, "action", None) != AI_PAPER_ACTION or body.get("action") != "ENTER":
            return None
        version = body.get("deployment_version")
        if version is None:
            return None
        row = conn.execute("SELECT state FROM authored_strategies WHERE deployment_version = ?", [version]).fetchone()
        if row is None:
            return None                                   # a catalogue version
        if not self._config.enabled:
            return AUTHORED_STRATEGIES_DISABLED
        return None if row[0] == DEPLOYED.value else "DEPLOYMENT_NOT_ACTIVE"

    def registration_refusal(self, deployment: AiDeployment, judgment_id: str, *, kind: str) -> None:
        """Spec 7.3, 7.9 and 8 before an authored deployment registers; raises DeploymentRefused."""
        digest = authored_digest_of_path(deployment.strategy_path)
        if digest != deployment.strategy_digest:
            raise DeploymentRefused("JUDGMENT_MISMATCH", "the authored path names another file than strategy_digest")
        if not self._config.enabled:
            raise DeploymentRefused(AUTHORED_STRATEGIES_DISABLED, "ai_paper.authored_strategies.enabled is false")
        record = self.get(digest)
        if record is None:
            raise DeploymentRefused(AUTHORED_STRATEGY_UNKNOWN, f"no authored strategy has digest {digest}")
        if not _may_register(record, judgment_id, deployment.class_name, kind):
            raise DeploymentRefused(AUTHORED_STATE_CONFLICT,
                                    f"the record is {record.state.value}, verdict {record.verdict} of "
                                    f"{record.judgment_id}; this is a {kind} registration of {judgment_id}")
        self._db.transaction(lambda conn: self.require_proven_in_tx(conn, deployment))

    def mark_deployed_in_tx(self, conn: Any, deployment: AiDeployment, judgment_id: str, version_digest: str,
                            now: dt.datetime) -> None:
        """Inside the seal transaction: SHADOW -> DEPLOYED, or the whole registration rolls back."""
        record = record_in_tx(conn, authored_digest_of_path(deployment.strategy_path))
        if record is None or not _may_register(record, judgment_id, deployment.class_name, "INITIAL"):
            raise DeploymentRefused(AUTHORED_STATE_CONFLICT, "the authored record moved before the seal")
        move_in_tx(conn, record, DEPLOYED, event="REGISTERED", reason=None, now=now,
                   deployment_version=version_digest)

    def entry_refusal(self, deployment: AiDeployment) -> Optional[str]:
        """Ruling 16, for an authored ENTER at admission and at final dispatch. Closes never ask."""
        if not self._config.enabled:
            return AUTHORED_STRATEGIES_DISABLED
        record = self.get(authored_digest_of_path(deployment.strategy_path))
        if record is None or record.state != DEPLOYED:
            return "DEPLOYMENT_NOT_ACTIVE"
        return None
```

- [ ] **Step 6: Gate the registration** (`trader/automation/ai_deployment_registration.py`)

Add `from trader.research.strategy_key import is_authored_path` to the imports. Replace the constructor with:

```python
    def __init__(self, *, journal: Any, deployments: Any, versions: Any, activity: Any, judgments: Any,
                 cooldowns: Any, bundles: Any, calendar: Any, expiry_sessions: int, now: Callable[[], dt.datetime],
                 authored: Any = None):
        """``journal`` is the trader's DomainJournal. A seal takes its write lock, as a withdrawal does, so a
        renewal and a withdrawal of its prior version commit one after the other (Plan 5). ``authored`` is the
        AuthoredStrategyStore (SP3 Plan 3): the staged-deploy gate of an authored deployment."""
        self._journal, self._deployments, self._versions, self._activity = journal, deployments, versions, activity
        self._judgments, self._cooldowns, self._bundles = judgments, cooldowns, bundles
        self._calendar, self._expiry_sessions, self._now = calendar, expiry_sessions, now
        self._authored = authored
```

In `register`, right after the `if bound is not None: ... return self._existing_outcome(...)` lines, add:

```python
        authored = is_authored_path(deployment.strategy_path)
        if authored:                                # SP3 spec 7.9: before any bundle read or seal
            self._authored_gate().registration_refusal(deployment, judgment_id, kind=judgment.kind)
```

Inside `write(conn)`, replace

```python
            digest, created = self._versions.seal_in_tx(conn, version, request_digest=digest_of_request,
                                                        principal=principal, command_id=command_id)
            return self._outcome(digest, version, created=created)
```

with

```python
            if authored:                           # review r1 #5: a failed run recorded after the preflight wins
                self._authored.require_proven_in_tx(conn, deployment)
            digest, created = self._versions.seal_in_tx(conn, version, request_digest=digest_of_request,
                                                        principal=principal, command_id=command_id)
            if authored and created and judgment.kind == INITIAL:
                self._authored.mark_deployed_in_tx(conn, deployment, judgment_id, digest, self._now())
            return self._outcome(digest, version, created=created)
```

and add the method:

```python
    def _authored_gate(self) -> Any:
        if self._authored is None:
            raise DeploymentRefused("AUTHORED_STRATEGIES_DISABLED", "this trader has no authored-strategy store")
        return self._authored
```

- [ ] **Step 7: Gate authored entries** (`trader/automation/ai_deployment_activity.py`)

Add the imports:

```python
from trader.automation.authored_refusals import AUTHORED_STRATEGIES_DISABLED
from trader.research.strategy_key import is_authored_path
```

Replace the constructor with:

```python
    def __init__(self, *, versions: Any, deployments: Any, judgments: Any, cooldowns: Any, max_active: int,
                 now: Callable[[], dt.datetime], authored: Any = None):
        """``authored`` is the AuthoredStrategyStore (SP3 Plan 3 ruling 16); an authored ENTER without it is refused."""
        if type(max_active) is not int or max_active < 0:
            raise ValueError("max_active must be an integer >= 0")
        self._versions, self._deployments = versions, deployments
        self._judgments, self._cooldowns = judgments, cooldowns
        self._max_active, self._now = max_active, now
        self._authored = authored
```

In `entry_refusal`, right after `deployment = self._deployments.get_sealed(deployment_digest)`, add:

```python
        if is_authored_path(deployment.strategy_path):
            code = self._authored_refusal(deployment)
            if code:
                return code
```

Replace `active` with:

```python
    def active(self) -> tuple[ActiveDeployment, ...]:
        chosen = []
        for digest, status in sorted(self.statuses().items()):
            if status != ACTIVE:
                continue
            version = self._versions.get(digest)
            deployment = self._deployments.get_sealed(version.base_digest)
            if is_authored_path(deployment.strategy_path) and self._authored_refusal(deployment):
                continue        # SP3 ruling 16: a retired or switched-off authored strategy is not loaded
            chosen.append(ActiveDeployment(digest, version, deployment))
        return tuple(chosen)
```

and add the method:

```python
    def _authored_refusal(self, deployment: AiDeployment) -> Optional[str]:
        if self._authored is None:
            return AUTHORED_STRATEGIES_DISABLED
        return self._authored.entry_refusal(deployment)
```

- [ ] **Step 8: Fence retirement at the broker-send boundary** (review r1 #4)

The entry gate of Step 7 reads outside the saga's SUBMITTING transaction, so a retirement could commit between it and the send. `send_gate_in_tx` (Step 5) reads the record on that transaction; the command stack adds it to the saga's send gate in Task 9. Give the AI paper test world the same gate. In `tests/automation/ai_paper_world.py`, set `self._authored_gate = None` in `__init__` next to `self._experiment_gate`, make `_send_gate_in_tx` end with

```python
        return self._withdrawal_gate(conn, request) or (
            None if self._experiment_gate is None else self._experiment_gate(conn, request)) or (
            None if self._authored_gate is None else self._authored_gate(conn, request))
```

and add:

```python
    def arm_authored(self, store_dir):
        """SP3 Plan 3: a DEPLOYED authored record bound to this world's version, and its SUBMITTING gate."""
        from tests.automation.authored_fixtures import submit_request
        from trader.automation.authored_config import AuthoredStrategiesConfig
        from trader.automation.authored_files import AuthoredFileStore
        from trader.automation.authored_schema import apply_authored_migrations
        from trader.automation.authored_strategies import (
            DEPLOYED, EVALUATING, SHADOW, AuthoredStrategyStore, move_in_tx, record_in_tx,
        )
        apply_authored_migrations(SchemaMigrator(self.db))
        store = AuthoredStrategyStore(self.db, config=AuthoredStrategiesConfig(enabled=True),
                                      files=AuthoredFileStore(store_dir), now=self.clock)
        digest = store.submit(submit_request())["record"]["digest"]
        steps = ((EVALUATING, {"evaluation_request_id": "sha256:" + "1" * 64}),
                 (SHADOW, {"verdict": "DEPLOY", "judgment_id": "jdg-00000001", "evaluation_request_id": None}),
                 (DEPLOYED, {"deployment_version": self.version_digest}))
        for target, changes in steps:
            self.db.transaction(lambda conn, target=target, changes=changes: move_in_tx(
                conn, record_in_tx(conn, digest), target, event="TEST", reason=None, now=self.clock(), **changes))
        self._authored_gate = store.send_gate_in_tx
        return store, digest
```

Write `tests/automation/test_authored_send_gate.py`:

```python
"""Review r1 #4: an authored retirement or switch-off after the final gate and before the send stops the entry."""
from __future__ import annotations

from dataclasses import replace

import pytest

from tests.automation.ai_paper_world import World
from trader.automation.authored_wire import RetireAuthoredStrategyRequest


@pytest.fixture
def world(tmp_path):
    return World(tmp_path, real_liquidation=True)


def retire(store, digest):
    return store.retire(RetireAuthoredStrategyRequest.model_validate(
        {"digest": digest, "code": "SANDBOX_ERROR", "detail": "live fault"}))


def test_a_deployed_authored_version_still_enters(world, tmp_path):
    world.arm_authored(tmp_path / "store")
    assert world.submit().state == "SUBMITTED" and len(world.dispatch.plans) == 1


def test_a_retirement_after_the_final_gate_and_before_the_send_stops_the_entry(world, tmp_path):
    store, digest = world.arm_authored(tmp_path / "store")
    real = world.guard.revalidate

    def gate_then_retire(*args, **kwargs):
        permit = real(*args, **kwargs)                  # the final gate passed: the record was DEPLOYED
        assert retire(store, digest)["record"]["state"] == "RETIRED"
        return permit
    world.guard.revalidate = gate_then_retire
    receipt = world.submit()
    assert (receipt.state, receipt.error_code) == ("REJECTED", "DEPLOYMENT_NOT_ACTIVE")
    assert world.dispatch.plans == []                     # no broker send


def test_a_switch_off_after_the_final_gate_and_before_the_send_stops_the_entry(world, tmp_path):
    store, _digest = world.arm_authored(tmp_path / "store")
    real = world.guard.revalidate

    def gate_then_switch_off(*args, **kwargs):
        permit = real(*args, **kwargs)
        store._config = replace(store.config, enabled=False)   # what a restart with the switch off reads
        return permit
    world.guard.revalidate = gate_then_switch_off
    receipt = world.submit()
    assert (receipt.state, receipt.error_code) == ("REJECTED", "AUTHORED_STRATEGIES_DISABLED")
    assert world.dispatch.plans == []


def test_a_catalogue_version_is_not_touched_by_the_authored_gate(world, tmp_path):
    store, _digest = world.arm_authored(tmp_path / "other-store")
    world.db.execute("UPDATE authored_strategies SET deployment_version = NULL")   # no authored row binds it
    store._config = replace(store.config, enabled=False)
    assert world.submit().state == "SUBMITTED"
```

- [ ] **Step 9: Run the tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/automation/test_authored_isolation_gate.py tests/automation/test_authored_send_gate.py tests/automation/test_ai_paper_version_recheck.py tests/automation/test_ai_deployment_registration.py tests/automation/test_ai_deployment_activity.py tests/automation/test_ai_deployment_rpc.py tests/automation/test_authored_lifecycle.py -q --timeout=60`
Expected: PASS.

- [ ] **Step 10: Commit**

```bash
git add trader/automation/authored_isolation.py trader/automation/authored_strategies.py \
  trader/automation/ai_deployment_registration.py trader/automation/ai_deployment_activity.py \
  tests/automation/authored_fixtures.py tests/automation/test_authored_isolation_gate.py \
  tests/automation/ai_paper_world.py tests/automation/test_authored_send_gate.py
git commit -m "feat: hold authored registrations until the running sandbox image is proven

The switch also refuses authored entries at admission and dispatch; closes never ask it.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---
### Task 9: The seven methods on the trader and their wiring

**Files:**
- Create: `trader/messaging/authored_surface.py`
- Modify: `trader/trading/command_stack.py`, `trader/messaging/production_api.py`
- Test: `tests/automation/test_authored_surface.py`, `tests/automation/test_authored_wiring.py`

**Interfaces:**
- Consumes: `AuthoredStrategyStore` (Tasks 5-8), `IsolationProofs` (Task 8), the wire models (Task 5), `RpcCaller`, `_DispatchProblem`, `TypedRpcRegistry.register`; test helpers `ServedStack`, `make_identities`, `ALLOW_ALL`, `build_full_production_registry` (`tests/rpc_identity_fixtures.py`), `served_stack` (`tests/sp1_fixtures.py`).
- Produces: `register_authored_surface(registry, *, authored, proofs) -> None`; `STORE_UNAVAILABLE = "AUTHORED_STORE_UNAVAILABLE"`; `AiPaperServices.authored`, `AiPaperServices.proofs`; `_AiPaperParts.authored`, `_AiPaperParts.proofs`.

- [ ] **Step 1: Write the failing tests**

`tests/automation/test_authored_surface.py`:

```python
"""SP3 Plan 3 Task 9: the seven methods over the real signed trader server (spec 5, 10 Keys and Authority)."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from tests.automation.authored_fixtures import (
    IMAGE, OTHER_IMAGE, authored_journal, authored_world, deployed, evaluating, proof_request, request_id, stored,
    submit_request,
)
from tests.automation.backtest_judge_fixtures import NOW, Clock
from tests.rpc_identity_fixtures import ALLOW_ALL, ServedStack, build_full_production_registry, make_identities
from trader.automation.authored_files import AuthoredFileStore, AuthoredStoreError
from trader.automation.authored_isolation import IsolationProofs
from trader.messaging.authored_surface import register_authored_surface
from trader.messaging.principals import SERVER_ACCEPTS, TRADER_ACL
from trader.messaging.typed_rpc import TypedRpcRegistry, TypedRpcRemoteError

ZERO = "sha256:" + "0" * 64
ROWS = {
    ("command", "submit_authored_strategy"): {"ai_author"},
    ("query", "get_authored_strategies"): {"ai_author"},
    ("query", "list_authored_strategies"): {"cli", "dashboard"},
    ("command", "report_authored_evaluation"): {"research"},
    ("command", "retire_authored_strategy"): {"strategy"},
    ("command", "record_isolation_proof"): {"cli"},
    ("query", "get_isolation_proof"): {"cli", "strategy"},
}
VALID_BODIES = {
    "submit_authored_strategy": lambda: submit_request().model_dump(),
    "get_authored_strategies": dict,
    "list_authored_strategies": dict,
    "report_authored_evaluation": lambda: {"digest": ZERO, "request_id": ZERO, "event": "SANDBOX_ERROR",
                                           "sandbox_image_digest": None, "detail": ""},
    "retire_authored_strategy": lambda: {"digest": ZERO, "code": "SANDBOX_ERROR", "detail": ""},
    "record_isolation_proof": lambda: proof_request().model_dump(),
    "get_isolation_proof": lambda: {"image_digest": None},
}


def role_of(method: str) -> str:
    return "command" if ("command", method) in TRADER_ACL else "query"


def serve(tmp_path, *, acl=TRADER_ACL, **config):
    proofs = IsolationProofs(authored_journal(tmp_path), now=Clock(NOW))
    world = authored_world(tmp_path, proofs=proofs, **config)
    registry = TypedRpcRegistry(acl=acl, default_execution="thread")
    register_authored_surface(registry, authored=world.store, proofs=proofs)
    stack = ServedStack({("trader", "command"): registry, ("trader", "query"): registry}, make_identities())

    def call(principal: str, method: str, body: dict) -> dict:
        return stack.client(principal, role=role_of(method)).call(method, body, dict)
    return SimpleNamespace(world=world, proofs=proofs, stack=stack, call=call)


@pytest.fixture
def served(tmp_path):
    setup = serve(tmp_path)
    yield setup
    setup.stack.close()


def test_every_method_is_refused_for_every_caller_outside_its_row(served):               # spec 10 Keys
    for (role, method), allowed in ROWS.items():
        assert TRADER_ACL[(role, method)] == allowed
        for principal in sorted(SERVER_ACCEPTS["trader"] - allowed):
            with pytest.raises(TypedRpcRemoteError) as exc:
                served.stack.client(principal, role=role).call(method, {}, dict)
            assert exc.value.code == "PERMISSION_DENIED", (principal, method)


def test_each_handler_refuses_a_wrong_caller_even_behind_an_open_allow_list(tmp_path):
    setup = serve(tmp_path, acl=ALLOW_ALL)
    try:
        for (role, method), allowed in ROWS.items():
            for principal in sorted(SERVER_ACCEPTS["trader"] - allowed):
                with pytest.raises(TypedRpcRemoteError) as exc:
                    setup.stack.client(principal, role=role).call(method, VALID_BODIES[method](), dict)
                assert exc.value.code == "PERMISSION_DENIED", (principal, method)
    finally:
        setup.stack.close()


def test_ai_author_stores_reads_back_and_resubmits_over_signed_rpc(served):
    first = served.call("ai_author", "submit_authored_strategy", submit_request().model_dump())
    assert (first["status"], first["record"]["state"]) == ("STORED", "STORED")
    assert served.call("ai_author", "submit_authored_strategy", submit_request().model_dump())["status"] == "EXISTING"
    view = served.call("ai_author", "get_authored_strategies", {})
    assert (view["counted"], view["room"]) == (1, 2)
    assert [record["digest"] for record in view["records"]] == [first["record"]["digest"]]
    assert served.call("cli", "list_authored_strategies", {}) == view


def test_a_refusal_is_a_reply_body_not_an_rpc_error(tmp_path):                         # spec 10 Authority
    setup = serve(tmp_path, enabled=False)
    try:
        reply = setup.call("ai_author", "submit_authored_strategy", submit_request().model_dump())
        assert (reply["status"], reply["code"], reply["record"]) == ("REFUSED", "AUTHORED_STRATEGIES_DISABLED", None)
    finally:
        setup.stack.close()


def test_a_malformed_body_is_a_validation_error(served):
    with pytest.raises(TypedRpcRemoteError) as exc:
        served.call("ai_author", "submit_authored_strategy", {"source": "x"})
    assert exc.value.code == "VALIDATION_ERROR"


def test_research_reports_and_the_strategy_retires_over_signed_rpc(served):
    digest = stored(served.world)
    evaluating(served.world, digest, request_id(1))
    reply = served.call("research", "report_authored_evaluation", {
        "digest": digest, "request_id": request_id(1), "event": "STARTED", "sandbox_image_digest": IMAGE,
        "detail": ""})
    assert (reply["status"], reply["record"]["sandbox_image_digest"], reply["judged_count"]) == ("UPDATED", IMAGE, 0)
    live = deployed(served.world, 16)
    retired = served.call("strategy", "retire_authored_strategy",
                          {"digest": live, "code": "SANDBOX_ERROR", "detail": "child crashed"})
    assert (retired["status"], retired["record"]["state"]) == ("UPDATED", "RETIRED")


def test_the_operator_records_a_proof_and_the_strategy_reads_it(served):                   # ruling 13
    recorded = served.call("cli", "record_isolation_proof", proof_request().model_dump())
    assert recorded["status"] == "RECORDED"
    assert served.call("strategy", "get_isolation_proof", {"image_digest": IMAGE}) == {
        "found": True, "proof": recorded["proof"]}
    assert served.call("cli", "get_isolation_proof", {"image_digest": OTHER_IMAGE}) == {"found": False, "proof": None}


def test_an_unusable_store_is_an_rpc_error(served, monkeypatch):
    def broken(self, digest, source, meta):
        raise AuthoredStoreError("/store is not a real directory")
    monkeypatch.setattr(AuthoredFileStore, "write", broken)
    with pytest.raises(TypedRpcRemoteError) as exc:
        served.call("ai_author", "submit_authored_strategy", submit_request().model_dump())
    assert exc.value.code == "AUTHORED_STORE_UNAVAILABLE"


def test_the_production_registry_registers_the_seven_methods():
    registered = {(r.socket_role, r.method) for r in build_full_production_registry().registrations()}
    assert set(ROWS) <= registered
```

`tests/automation/test_authored_wiring.py`:

```python
"""SP3 Plan 3 Task 9: the trader wires one authored store into SP2c and serves it (spec 5, 7.3)."""
from __future__ import annotations

from dataclasses import replace

import pytest

from tests.automation.authored_fixtures import submit_request
from tests.sp1_acceptance.conftest import loop_thread  # noqa: F401
from tests.sp1_fixtures import served_stack
from trader.automation.authored_config import AuthoredStrategiesConfig
from trader.automation.authored_files import STORE_DIR_ENV
from trader.automation.authored_isolation import IsolationProofs
from trader.automation.authored_strategies import AuthoredStrategyStore
from trader.data.schema_migrations import SchemaMigrator
from trader.messaging.typed_rpc import TypedRpcRemoteError


def switch_on(trader) -> None:
    """trader.yaml with ai_paper.authored_strategies.enabled: true, as the owner sets it."""
    trader.ai_paper_config = replace(trader.ai_paper_config,
                                     authored_strategies=AuthoredStrategiesConfig(enabled=True))


@pytest.fixture
def served(tmp_path, loop_thread, monkeypatch):
    monkeypatch.setenv(STORE_DIR_ENV, str(tmp_path / "authored_store"))
    stack = served_stack(tmp_path, loop_thread, monkeypatch, prepare=switch_on)
    yield stack
    stack.close()


def test_the_trader_wires_one_authored_store_everywhere(served):
    ai_paper = served.composed.stack.ai_paper
    assert isinstance(ai_paper.authored, AuthoredStrategyStore) and isinstance(ai_paper.proofs, IsolationProofs)
    assert ai_paper.claims._authored is ai_paper.authored
    assert ai_paper.judgments._authored is ai_paper.authored
    assert ai_paper.activity._authored is ai_paper.authored is ai_paper.registrar._authored
    assert {130, 131} <= set(SchemaMigrator(served.trader.journal_db).applied_versions())


def test_ai_author_submits_through_the_production_registry(served, tmp_path):
    reply = served.call("ai_author", "submit_authored_strategy", submit_request().model_dump())
    assert reply["status"] == "STORED"
    assert list((tmp_path / "authored_store").glob("*.py"))
    view = served.call("cli", "list_authored_strategies", {})
    assert (view["enabled"], view["counted"]) == (True, 1)
    with pytest.raises(TypedRpcRemoteError) as exc:
        served.call("ai_research", "submit_authored_strategy", submit_request().model_dump())
    assert exc.value.code == "PERMISSION_DENIED"
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/automation/test_authored_surface.py tests/automation/test_authored_wiring.py -q --timeout=120`
Expected: FAIL (`ModuleNotFoundError: trader.messaging.authored_surface`).

- [ ] **Step 3: Write the surface** (`trader/messaging/authored_surface.py`)

```python
"""SP3 authored-strategy methods on the trader (spec 5, 7.3, 7.9; Plan 3 rulings 13-14).

Direct handlers: no command ledger, no controller epoch. The allow-list is not the authority; each handler
checks its caller again. A business refusal is a reply body; an unusable store directory is an RPC error.
"""
from __future__ import annotations

from typing import Any, Callable

from trader.automation.authored_files import AuthoredStoreError
from trader.automation.authored_wire import (
    GetAuthoredStrategiesRequest, GetIsolationProofRequest, RecordIsolationProofRequest,
    ReportAuthoredEvaluationRequest, RetireAuthoredStrategyRequest, SubmitAuthoredStrategyRequest,
)
from trader.messaging.typed_rpc import RpcCaller, _DispatchProblem

AI_AUTHOR = frozenset({"ai_author"})
HUMAN_READERS = frozenset({"cli", "dashboard"})
RESEARCH = frozenset({"research"})
STRATEGY = frozenset({"strategy"})
OPERATOR = frozenset({"cli"})
PROOF_READERS = frozenset({"cli", "strategy"})
STORE_UNAVAILABLE = "AUTHORED_STORE_UNAVAILABLE"


def _require(caller: RpcCaller, allowed: frozenset[str], method: str) -> None:
    if caller.principal not in allowed:
        raise _DispatchProblem("PERMISSION_DENIED", f"principal {caller.principal!r} may not call {method!r}")


def _loudly(work: Callable[[], dict]) -> dict:
    try:
        return work()
    except AuthoredStoreError as exc:               # an unusable store is an error, never a refusal
        raise _DispatchProblem(STORE_UNAVAILABLE, str(exc)) from None


def register_authored_surface(registry: Any, *, authored: Any, proofs: Any) -> None:
    def submit_authored_strategy(parsed: SubmitAuthoredStrategyRequest, caller: RpcCaller) -> dict:
        _require(caller, AI_AUTHOR, "submit_authored_strategy")
        return _loudly(lambda: authored.submit(parsed))

    def get_authored_strategies(parsed: GetAuthoredStrategiesRequest, caller: RpcCaller) -> dict:
        _require(caller, AI_AUTHOR, "get_authored_strategies")
        return authored.overview()

    def list_authored_strategies(parsed: GetAuthoredStrategiesRequest, caller: RpcCaller) -> dict:
        _require(caller, HUMAN_READERS, "list_authored_strategies")
        return authored.overview()

    def report_authored_evaluation(parsed: ReportAuthoredEvaluationRequest, caller: RpcCaller) -> dict:
        _require(caller, RESEARCH, "report_authored_evaluation")
        return authored.report(parsed)

    def retire_authored_strategy(parsed: RetireAuthoredStrategyRequest, caller: RpcCaller) -> dict:
        _require(caller, STRATEGY, "retire_authored_strategy")
        return authored.retire(parsed)

    def record_isolation_proof(parsed: RecordIsolationProofRequest, caller: RpcCaller) -> dict:
        _require(caller, OPERATOR, "record_isolation_proof")
        return proofs.record(parsed, principal=caller.principal)

    def get_isolation_proof(parsed: GetIsolationProofRequest, caller: RpcCaller) -> dict:
        _require(caller, PROOF_READERS, "get_isolation_proof")
        proof = proofs.latest(parsed.image_digest)
        return {"found": proof is not None, "proof": None if proof is None else proof.to_json()}

    methods = (
        ("command", "submit_authored_strategy", SubmitAuthoredStrategyRequest, submit_authored_strategy),
        ("query", "get_authored_strategies", GetAuthoredStrategiesRequest, get_authored_strategies),
        ("query", "list_authored_strategies", GetAuthoredStrategiesRequest, list_authored_strategies),
        ("command", "report_authored_evaluation", ReportAuthoredEvaluationRequest, report_authored_evaluation),
        ("command", "retire_authored_strategy", RetireAuthoredStrategyRequest, retire_authored_strategy),
        ("command", "record_isolation_proof", RecordIsolationProofRequest, record_isolation_proof),
        ("query", "get_isolation_proof", GetIsolationProofRequest, get_isolation_proof),
    )
    for role, method, model, handler in methods:
        registry.register(role, method, model, dict, handler, execution="thread", with_caller=True)
```

- [ ] **Step 4: Register it on an ai_paper stack** (`trader/messaging/production_api.py`)

After the line `_register_backtest_judge(registry, ai_paper)` add `_register_authored(registry, ai_paper)`, and add next to `_register_backtest_judge`:

```python
def _register_authored(registry: TypedRpcRegistry, ai_paper) -> None:
    """SP3 Plan 3: authored strategies, only on an enabled ai_paper stack, which is paper only."""
    from trader.messaging.authored_surface import register_authored_surface
    missing = [name for name in ("authored", "proofs") if getattr(ai_paper, name, None) is None]
    if missing:
        raise RuntimeError(f"the ai_paper stack is missing its authored-strategy services: {', '.join(missing)}")
    register_authored_surface(registry, authored=ai_paper.authored, proofs=ai_paper.proofs)
```

- [ ] **Step 5: Wire one store through the stack** (`trader/trading/command_stack.py`)

In `AiPaperServices`, after `registrar`, add:

```python
    authored: Any = None          # AuthoredStrategyStore (SP3 Plan 3)
    proofs: Any = None            # IsolationProofs (SP3 Plan 3)
```

In `_AiPaperParts`, after `forward_evidence`, add:

```python
    authored: Any = None           # AuthoredStrategyStore (SP3 Plan 3): claims, judgments, registration and entries
    proofs: Any = None             # IsolationProofs (SP3 Plan 3)
```

In `_build_ai_paper_parts`, add the imports next to the others:

```python
    from trader.automation.authored_files import AuthoredFileStore, default_store_dir
    from trader.automation.authored_isolation import IsolationProofs
    from trader.automation.authored_strategies import AuthoredStrategyStore, DeploymentLines, withdraw_with
```

right after `bundles = ResearchBundleCheck(...)` add:

```python
    proofs = IsolationProofs(trader.journal_db, now=aware_now)
    # SP3 Plan 3: the authored-strategy authority. Claims, judgments, registration and the entry gate ask it;
    # its expiry reads the deployment lines, built below (the lambda runs only after they exist).
    authored = AuthoredStrategyStore(
        trader.journal_db, config=config.authored_strategies, files=AuthoredFileStore(default_store_dir()),
        now=aware_now, proofs=proofs, calendar=calendar, shadow_sessions=judge.deploy_expiry_sessions,
        lines=lambda: DeploymentLines(activity=activity, versions=versions),
        withdraw=lambda version_digest: withdraw_with(versions)(version_digest))
```

then pass `authored=authored` to the three constructors that take it:

```python
    backtest_judgments = BacktestJudgments(trader.journal_db, config=judge, calendar=calendar, cases_dir=cases_dir,
                                           verify_dir=verify_dir, now=now,
                                           renewals=TraderRenewalChecks(forward=forward_evidence, gate=gate),
                                           authored=authored)
```

```python
    activity = DeploymentActivity(versions=versions, deployments=deployments, judgments=judgments,
                                  cooldowns=cooldowns, max_active=judge.max_active_deploys, now=aware_now,
                                  authored=authored)
    registrar = AiDeploymentRegistrar(
        journal=trader.domain_journal, deployments=deployments, versions=versions, activity=activity,
        judgments=judgments, cooldowns=cooldowns, bundles=bundles, calendar=calendar,
        expiry_sessions=judge.deploy_expiry_sessions, now=aware_now, authored=authored)
```

and end the `_AiPaperParts(...)` return with `forward_evidence=forward_evidence, authored=authored, proofs=proofs)`.

In `_build_ai_paper_services`, build the claims with the store and pass both on:

```python
    claims = EvaluationClaims(trader.journal_db, config=parts.config.backtest_judge, now=now, authored=parts.authored)
```

and end the `AiPaperServices(...)` return with `activity=parts.activity, registrar=parts.registrar, authored=parts.authored, proofs=parts.proofs)`.

In `_ai_paper_send_gate_in_tx` (review r1 #4), after `gates = [deployment_withdrawal_gate_in_tx(versions=parts.versions)]`:

```python
    if getattr(parts, "authored", None) is not None:
        gates.append(parts.authored.send_gate_in_tx)    # SP3: a retired or switched-off authored version
```

and append to `tests/automation/test_authored_send_gate.py`:

```python
def test_the_command_stack_hands_the_saga_the_authored_check(world, tmp_path):
    from types import SimpleNamespace

    from trader.automation.ai_paper_decision import AI_PAPER_ACTION
    from trader.trading.command_stack import _AiPaperParts, _ai_paper_send_gate_in_tx

    store, digest = world.arm_authored(tmp_path / "store")
    parts = _AiPaperParts(config=None, policy=world.policy, entry_filter=world.entry_filter,
                          deployments=world.deployments, scope_checks=None, filter_refusal=None,
                          versions=world.versions, activity=world.activity, authored=store)
    gate = _ai_paper_send_gate_in_tx(parts)
    request = SimpleNamespace(action=AI_PAPER_ACTION, body=world.body())
    assert world.db.transaction(lambda conn: gate(conn, request)) is None
    retire(store, digest)
    assert world.db.transaction(lambda conn: gate(conn, request)) == "DEPLOYMENT_NOT_ACTIVE"
```

- [ ] **Step 6: Run the tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/automation/test_authored_surface.py tests/automation/test_authored_wiring.py tests/automation/test_authored_send_gate.py tests/automation/test_ai_paper_version_recheck.py tests/automation/test_renewal_wiring.py tests/automation/test_backtest_judge_surface.py tests/test_rpc_acl.py -q --timeout=120`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add trader/messaging/authored_surface.py trader/messaging/production_api.py trader/trading/command_stack.py \
  tests/automation/test_authored_surface.py tests/automation/test_authored_wiring.py \
  tests/automation/test_authored_send_gate.py
git commit -m "feat: serve the authored-strategy methods and wire one store through sp2c

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---
### Task 10: `mmr ai-author list` and `mmr ai-author prove-isolation`

Needs SP3 Plan 2 merged (`trader/authored/sandbox_client.py`, `trader/sandbox/isolation_checks.py`, `trader/sandbox/protocol.py`). Rebase on it first.

The flow (SP3 decision D3): the host command runs `<compose> exec -T research mmr ai-author prove-isolation --in-container`. Inside the `research` container (it mounts the research socket) that step calls Plan 2's `run_isolation_checks` over `RESEARCH_SOCKET` with probe sessions (`open_probe`), prints one proof body as JSON and holds no key. The host command checks the JSON against the trader's wire model and records it with `record_isolation_proof` as `cli`. There is no `docker inspect`: the build id in `PONG` comes from the image itself (Plan 2 ruling 1), and the posture in `PONG` covers capabilities, seccomp, no-new-privileges, network, environment, read-only root and the pids limit (Plan 2 ruling 27; decision D2).

**Files:**
- Create: `trader/authored/prove_isolation.py` (the in-container step), `trader/authored/isolation_host.py` (the host step)
- Modify: `trader/sdk.py`, `trader/mmr_cli.py`, `docs/CLI_REFERENCE.md`, `AGENTS.md`
- Test: `tests/authored/test_prove_isolation.py` (Plan 1 creates `tests/authored/`; create the directory if it is missing), `tests/test_ai_author_cli.py`, `tests/fullstack/test_prove_isolation_in_research.py` (container test, Plan 2's fullstack runner only)

**Interfaces:**
- Consumes: Plan 2 `SandboxClient`, `SandboxUnavailable`, `run_isolation_checks`, `RESEARCH_SOCKET`, `BUILD_ID_RE`; `RecordIsolationProofRequest`, `IMAGE_ID` (Task 5); `running_in_container` (`trader/messaging/keys_cli.py`); `repo_root` (`trader/research/strategy_paths.py`).
- Produces: `prove_isolation.run_in_container(socket_path=RESEARCH_SOCKET, out=sys.stdout, now=...) -> int` (exit 3 `LIVE_SESSIONS_OPEN` while `PONG.live_open` is not 0 before or after the run; owner ruling 2026-10-11), `LIVE_SESSIONS_OPEN`, `EXIT_LIVE_SESSIONS_OPEN`, `prove_isolation.result_body(result, ran_at) -> dict`; `isolation_host.ProveIsolationError`, `compose_command(text: str) -> list[str]`, `in_container_command(compose: list[str]) -> list[str]`, `parse_proof(stdout: str) -> dict`, `prove_isolation(compose: list[str], run=run_command) -> dict`; `MMR.authored_strategies() -> dict`, `MMR.record_isolation_proof(proof: dict) -> dict`, `MMR.isolation_proof(image_digest=None) -> dict`; CLI `ai-author list|prove-isolation [--compose TEXT] [--in-container]`.

- [ ] **Step 1: Write the failing tests**

`tests/authored/test_prove_isolation.py`:

```python
"""SP3 Plan 3 Task 10: the isolation run inside research and the host record step (spec 7.9; D2, D3; rulings 15, 22)."""
from __future__ import annotations

import datetime as dt
import io
import json
from types import SimpleNamespace

import pytest

import trader.authored.prove_isolation as runner
from trader.authored.isolation_host import (
    ProveIsolationError, compose_command, in_container_command, parse_proof, prove_isolation,
)
from trader.authored.sandbox_client import SandboxUnavailable
from trader.automation.authored_wire import IMAGE_ID, RecordIsolationProofRequest
from trader.sandbox.protocol import BUILD_ID_RE

IMAGE = "a1b2c3d-20261011T220000Z"                      # a Plan 2 build id
RAN_AT = dt.datetime(2026, 10, 11, 22, 0, tzinfo=dt.timezone.utc)
PROOF = {"image_digest": IMAGE, "passed": True, "ran_at": RAN_AT.isoformat(),
         "checks": [{"name": "escape_pd_io_common_oskill", "passed": True, "detail": "contained: SANDBOX_ERROR"},
                    {"name": "read_only_root", "passed": True, "detail": "root_read_only=True"}]}


def test_the_trader_and_the_sandbox_spell_a_build_id_the_same_way():
    assert IMAGE_ID.pattern == BUILD_ID_RE.pattern                  # one spelling (D2)


def test_the_authored_cap_never_exceeds_the_live_sandbox_pool():
    from trader.automation.authored_config import MAX_ACTIVE_RANGE
    from trader.sandbox.supervisor import LIVE_POOL_SIZE

    assert MAX_ACTIVE_RANGE[1] == LIVE_POOL_SIZE                    # controller decision C2


class FakeSandbox:
    """PING answers with ``live_open`` from the list, one value per call (the last repeats)."""

    def __init__(self, *live_open):
        self.live_open = list(live_open or (0,))

    def ping(self):
        value = self.live_open.pop(0) if len(self.live_open) > 1 else self.live_open[0]
        return SimpleNamespace(image_digest=IMAGE, live_open=value)


def test_the_runner_prints_one_proof_body(monkeypatch):
    result = SimpleNamespace(passed=True, image_digest=IMAGE, checks=(
        SimpleNamespace(name="escape_fork_bomb", passed=True, detail="contained: SANDBOX_ERROR"),))
    seen = []
    monkeypatch.setattr(runner, "SandboxClient", lambda path: seen.append(path) or FakeSandbox(0))
    monkeypatch.setattr(runner, "run_isolation_checks", lambda client: result)
    out = io.StringIO()
    assert runner.run_in_container(out=out, now=lambda: RAN_AT) == 0
    assert seen == [runner.RESEARCH_SOCKET]                         # the research pool, never a live slot
    assert json.loads(out.getvalue()) == {
        "image_digest": IMAGE, "passed": True, "ran_at": RAN_AT.isoformat(),
        "checks": [{"name": "escape_fork_bomb", "passed": True, "detail": "contained: SANDBOX_ERROR"}]}


@pytest.mark.parametrize("live_open", [(1,), (None,), (0, 2)])          # before, unknown, opened during the run
def test_the_run_refuses_while_a_live_session_is_open(monkeypatch, capsys, live_open):   # owner ruling 2026-10-11
    ran = []
    monkeypatch.setattr(runner, "SandboxClient", lambda path: FakeSandbox(*live_open))
    monkeypatch.setattr(runner, "run_isolation_checks", lambda client: ran.append(1) or SimpleNamespace(
        passed=True, image_digest=IMAGE, checks=()))
    out = io.StringIO()
    assert runner.run_in_container(out=out) == runner.EXIT_LIVE_SESSIONS_OPEN
    assert out.getvalue() == "" and "LIVE_SESSIONS_OPEN" in capsys.readouterr().err
    assert ran == ([] if live_open[0] != 0 else [1])


def test_an_unreachable_sandbox_exits_2_and_prints_nothing(monkeypatch):
    def down(client):
        raise SandboxUnavailable("no socket")
    monkeypatch.setattr(runner, "SandboxClient", lambda path: FakeSandbox(0))
    monkeypatch.setattr(runner, "run_isolation_checks", down)
    out = io.StringIO()
    assert runner.run_in_container(out=out) == 2
    assert out.getvalue() == ""


def test_the_host_runs_the_in_container_step_once_and_returns_the_checked_proof():
    calls = []

    def run(argv):
        calls.append(argv)
        return "a warning line\n" + json.dumps(PROOF) + "\n"
    compose = ["docker", "compose", "-f", "/repo/docker-compose.yml"]
    proof = prove_isolation(compose, run=run)
    assert calls == [compose + ["exec", "-T", "research", "mmr", "ai-author", "prove-isolation", "--in-container"]]
    assert calls[0] == in_container_command(compose)
    assert proof == PROOF


def test_a_failing_check_gives_a_failing_proof_that_the_trader_accepts():
    failing = {**PROOF, "passed": False,
               "checks": [{**PROOF["checks"][0], "passed": False, "detail": "ran to completion: NOT contained"}]}
    assert parse_proof(json.dumps(failing))["passed"] is False
    RecordIsolationProofRequest.model_validate(failing)


@pytest.mark.parametrize("stdout, hint", [
    ("", "printed nothing"),
    ("not json\n", "did not print JSON"),
    (json.dumps({**PROOF, "passed": True,
                 "checks": [{**PROOF["checks"][0], "passed": False}]}), "not a valid proof"),
    (json.dumps({**PROOF, "image_digest": "unset"}), "not a valid proof"),
    (json.dumps({**PROOF, "docker_inspect": {}}), "not a valid proof"),
])
def test_output_that_is_not_a_valid_proof_is_an_error(stdout, hint):
    with pytest.raises(ProveIsolationError, match=hint):
        parse_proof(stdout)


def test_compose_command_names_the_repo_compose_file_and_its_runtime():
    command = compose_command("docker compose")
    assert command[:3] == ["docker", "compose", "-f"] and command[3].endswith("docker-compose.yml")
    assert compose_command("podman-compose")[0] == "podman-compose"
```

`tests/test_ai_author_cli.py`:

```python
"""SP3 Plan 3 Task 10: mmr ai-author list | prove-isolation [--in-container] (spec 7.9; D3; rulings 14-15)."""
from __future__ import annotations

import argparse
import sys
from types import SimpleNamespace

import pytest

import trader.authored.isolation_host as host
import trader.authored.prove_isolation as runner
import trader.messaging.keys_cli as keys_cli
import trader.mmr_cli as mmr_cli
from trader.mmr_cli import _LOCAL_ONLY_COMMANDS, _handle_ai_author, build_parser

PROOF = {"image_digest": "a1b2c3d-20261011T220000Z", "passed": True, "ran_at": "2026-10-11T22:00:00+00:00",
         "checks": [{"name": "network_none", "passed": True, "detail": "interfaces=['lo']"}]}


def args(action, in_container=False, **extra):
    return argparse.Namespace(ai_author_action=action, compose="docker compose", in_container=in_container,
                              **extra)


def test_the_parser_knows_both_actions_and_the_in_container_step():
    parser = build_parser()
    assert parser.parse_args(["ai-author", "list"]).ai_author_action == "list"
    parsed = parser.parse_args(["ai-author", "prove-isolation", "--compose", "podman compose"])
    assert (parsed.ai_author_action, parsed.compose, parsed.in_container) == (
        "prove-isolation", "podman compose", False)
    assert parser.parse_args(["ai-author", "prove-isolation", "--in-container"]).in_container is True
    assert "ai-author" in _LOCAL_ONLY_COMMANDS                    # typed RPC only, no legacy connect


def test_list_prints_the_records(capsys):
    mmr = SimpleNamespace(authored_strategies=lambda: {"enabled": False, "counted": 0, "records": []})
    _handle_ai_author(mmr, args("list"))
    assert "counted" in capsys.readouterr().out


def test_the_host_step_refuses_inside_a_container(monkeypatch):
    monkeypatch.setattr(keys_cli, "running_in_container", lambda: True)
    with pytest.raises(SystemExit) as exit_:
        _handle_ai_author(SimpleNamespace(), args("prove-isolation"))
    assert exit_.value.code == 2


def test_the_in_container_step_refuses_on_the_host(monkeypatch):
    monkeypatch.setattr(keys_cli, "running_in_container", lambda: False)
    with pytest.raises(SystemExit) as exit_:
        mmr_cli._prove_isolation_in_container()
    assert exit_.value.code == 2


def test_the_in_container_step_builds_no_sdk_and_needs_no_key(monkeypatch):
    monkeypatch.setattr(keys_cli, "running_in_container", lambda: True)
    monkeypatch.setattr(runner, "run_in_container", lambda: 0)
    monkeypatch.setattr(mmr_cli, "MMR", lambda: pytest.fail("the in-container step must not build an SDK"))
    monkeypatch.setattr(sys, "argv", ["mmr", "ai-author", "prove-isolation", "--in-container"])
    with pytest.raises(SystemExit) as exit_:
        mmr_cli.main()
    assert exit_.value.code == 0


def test_prove_isolation_records_the_proof_as_the_operator(monkeypatch):
    monkeypatch.setattr(keys_cli, "running_in_container", lambda: False)
    monkeypatch.setattr(host, "prove_isolation", lambda compose: PROOF)
    recorded = []
    mmr = SimpleNamespace(record_isolation_proof=lambda body: recorded.append(body) or {"status": "RECORDED",
                                                                                          "proof": body})
    _handle_ai_author(mmr, args("prove-isolation"))
    assert recorded == [PROOF]


def test_a_failing_proof_is_recorded_and_exits_1(monkeypatch):
    monkeypatch.setattr(keys_cli, "running_in_container", lambda: False)
    failing = {**PROOF, "passed": False, "checks": [{**PROOF["checks"][0], "passed": False}]}
    monkeypatch.setattr(host, "prove_isolation", lambda compose: failing)
    recorded = []
    mmr = SimpleNamespace(record_isolation_proof=lambda body: recorded.append(body) or {"status": "RECORDED"})
    with pytest.raises(SystemExit) as exit_:
        _handle_ai_author(mmr, args("prove-isolation"))
    assert exit_.value.code == 1 and recorded == [failing]
```

`tests/fullstack/test_prove_isolation_in_research.py` (runs only under Plan 2's `scripts/run_sandbox_fullstack.sh`, where the test override gives `research` the `test` profile and `sleep infinity`):

```python
"""SP3 Plan 3 Task 10 (D3): the in-container step runs in the real research container against the real sandbox."""
import json
import os
import socket as _socket
from pathlib import Path

import pytest

docker = pytest.importorskip("docker", reason="docker-py not installed")

from trader.automation.authored_wire import RecordIsolationProofRequest
from trader.sandbox import protocol as proto

pytestmark = pytest.mark.skipif(
    os.environ.get("MMR_FAKE_BROKER") != "1" or not Path(proto.RESEARCH_SOCKET).exists(),
    reason="only the sandbox fullstack runner mounts the sandbox sockets")


def _research_container(client):
    me = client.containers.get(_socket.gethostname())
    project = me.labels["com.docker.compose.project"]
    found = client.containers.list(filters={"label": [
        f"com.docker.compose.project={project}", "com.docker.compose.service=research"]})
    assert len(found) == 1, "expected one research container"
    return found[0]


def test_the_in_container_step_prints_a_passing_proof_the_trader_accepts():
    client = docker.from_env()
    try:
        result = _research_container(client).exec_run(["mmr", "ai-author", "prove-isolation", "--in-container"])
    finally:
        client.close()
    assert result.exit_code == 0, result.output.decode()[-2000:]
    proof = json.loads(result.output.decode().strip().splitlines()[-1])
    RecordIsolationProofRequest.model_validate(proof)
    failed = [check for check in proof["checks"] if not check["passed"]]
    assert proof["passed"] is True, failed
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/authored/test_prove_isolation.py tests/test_ai_author_cli.py -q --timeout=60`
Expected: FAIL (`ModuleNotFoundError: trader.authored.prove_isolation`). The fullstack test skips here.

- [ ] **Step 3: Write the in-container step** (`trader/authored/prove_isolation.py`)

```python
"""The in-container step of ``mmr ai-author prove-isolation`` (SP3 spec 7.9; decision D3; ruling 15).

The host runs ``mmr ai-author prove-isolation --in-container`` with ``docker compose exec -T research``: the
research container mounts the research socket, so the checks use the research pool and never a live slot.
This step prints one JSON proof body and holds no key; the host checks it and records it as ``cli``.
"""
from __future__ import annotations

import datetime as dt
import json
import sys
from typing import Any, Callable, TextIO

from trader.authored.sandbox_client import SandboxClient, SandboxUnavailable
from trader.sandbox.isolation_checks import run_isolation_checks
from trader.sandbox.protocol import RESEARCH_SOCKET

MAX_DETAIL_CHARS = 500
LIVE_SESSIONS_OPEN = "LIVE_SESSIONS_OPEN"
EXIT_LIVE_SESSIONS_OPEN = 3


def result_body(result: Any, ran_at: dt.datetime) -> dict:
    """A ``record_isolation_proof`` body: Plan 2's result, the build id from PONG and the run time."""
    return {"image_digest": result.image_digest, "passed": bool(result.passed), "ran_at": ran_at.isoformat(),
            "checks": [{"name": check.name, "passed": bool(check.passed),
                        "detail": str(check.detail)[:MAX_DETAIL_CHARS]} for check in result.checks]}


def run_in_container(socket_path: str = RESEARCH_SOCKET, out: TextIO = sys.stdout,
                     now: Callable[[], dt.datetime] = lambda: dt.datetime.now(dt.timezone.utc)) -> int:
    ran_at = now()
    client = SandboxClient(socket_path)
    try:
        if _live_sessions_open(client):
            return EXIT_LIVE_SESSIONS_OPEN
        result = run_isolation_checks(client)
        if _live_sessions_open(client):           # one opened during the run: the run proves nothing
            return EXIT_LIVE_SESSIONS_OPEN
    except SandboxUnavailable as exc:
        print(f"the sandbox at {socket_path} is unavailable: {exc}", file=sys.stderr)
        return 2
    out.write(json.dumps(result_body(result, ran_at)) + "\n")
    return 0


def _live_sessions_open(client: SandboxClient) -> bool:
    """Owner ruling 2026-10-11 (Plan 2 rulings 21, 29): no hostile probe runs next to a live strategy's child."""
    live_open = client.ping().live_open
    if live_open == 0:
        return False
    print(f"{LIVE_SESSIONS_OPEN}: the sandbox reports {live_open!r} open live session(s); turn "
          "ai_paper.authored_strategies.enabled off (or stop the strategy service), wait for the sessions to "
          "close and run again", file=sys.stderr)
    return True
```

- [ ] **Step 4: Write the host step** (`trader/authored/isolation_host.py`)

```python
"""Host step of ``mmr ai-author prove-isolation`` (SP3 spec 7.9; decisions D2, D3; rulings 15 and 22).

It runs the in-container step in the research container and checks its JSON against the trader's wire model.
It needs no key and runs no ``docker inspect``; the CLI signs the record as ``cli``.
"""
from __future__ import annotations

import json
import shlex
import subprocess
from typing import Callable

from pydantic import ValidationError

from trader.automation.authored_wire import RecordIsolationProofRequest
from trader.research.strategy_paths import repo_root

RUN_TIMEOUT_SECONDS = 900
Runner = Callable[[list[str]], str]


class ProveIsolationError(Exception):
    """The run could not finish (no sandbox, no output, a malformed proof); nothing is recorded."""


def compose_command(text: str) -> list[str]:
    """The compose command as docker.sh uses it, on this checkout's docker-compose.yml."""
    return [*shlex.split(text), "-f", str(repo_root() / "docker-compose.yml")]


def in_container_command(compose: list[str]) -> list[str]:
    return [*compose, "exec", "-T", "research", "mmr", "ai-author", "prove-isolation", "--in-container"]


def run_command(argv: list[str]) -> str:
    completed = subprocess.run(argv, capture_output=True, text=True, check=False, timeout=RUN_TIMEOUT_SECONDS)
    if completed.returncode != 0:
        raise ProveIsolationError(f"{' '.join(argv[:3])} ... exited {completed.returncode}: "
                                  f"{completed.stderr.strip()[-500:]}")
    return completed.stdout


def parse_proof(stdout: str) -> dict:
    """The last output line as a proof body, checked by the same model the trader uses (ruling 21)."""
    lines = [line for line in stdout.splitlines() if line.strip()]
    if not lines:
        raise ProveIsolationError("the isolation checks printed nothing")
    try:
        body = json.loads(lines[-1])
    except json.JSONDecodeError:
        raise ProveIsolationError("the isolation checks did not print JSON") from None
    try:
        RecordIsolationProofRequest.model_validate(body)
    except ValidationError as exc:
        raise ProveIsolationError(f"the isolation checks printed not a valid proof: {exc.errors()[:3]}") from None
    return body


def prove_isolation(compose: list[str], run: Runner = run_command) -> dict:
    return parse_proof(run(in_container_command(compose)))
```

- [ ] **Step 5: Add the SDK reads and the record** (`trader/sdk.py`, after `ai_deployment_version`)

```python
    def authored_strategies(self) -> dict:
        """``list_authored_strategies``: the PAPER authored-strategy records, the switch and the cap."""
        return self._typed_query.call('list_authored_strategies', {}, dict)

    def record_isolation_proof(self, proof: dict) -> dict:
        """``record_isolation_proof`` as the operator (``cli``): one run of ``mmr ai-author prove-isolation``."""
        return self._typed_command.call('record_isolation_proof', proof, dict)

    def isolation_proof(self, image_digest: Optional[str] = None) -> dict:
        """``get_isolation_proof``: the newest proof of ``image_digest``, or the newest of all."""
        return self._typed_query.call('get_isolation_proof', {'image_digest': image_digest}, dict)
```

- [ ] **Step 6: Add the CLI command** (`trader/mmr_cli.py`)

In `build_parser`, after the `ai-deployment` block (after `withdraw_p.add_argument('--reason', required=True)`), add:

```python
    # ai-author (SP3 Plan 3, spec 7.9): authored-strategy records and the isolation proof (operator)
    ai_author_p = sub.add_parser(
        'ai-author',
        help='PAPER AI-written strategies: list, prove-isolation (operator, host only)',
        epilog='Examples:\n'
               '  ai-author list\n'
               '  ai-author prove-isolation\n'
               '  ai-author prove-isolation --compose "podman compose"',
        formatter_class=fmt)
    ai_author_sub = ai_author_p.add_subparsers(dest='ai_author_action')
    ai_author_sub.add_parser('list', help='Authored strategies, their states, the switch and the cap')
    prove_p = ai_author_sub.add_parser(
        'prove-isolation', help='Run the isolation checks against the running sandbox and record them (host only)')
    prove_p.add_argument('--compose', default='docker compose', help='Compose command, as docker.sh uses it')
    prove_p.add_argument('--in-container', action='store_true',
                         help=argparse.SUPPRESS)   # the step the host runs inside research; prints JSON only
```

In `main()`, right after the `if cmd == 'keys':` line pair, add (the in-container step builds no SDK, so the research container needs no `cli` key):

```python
        if cmd == 'ai-author' and getattr(args, 'in_container', False):
            sys.exit(_prove_isolation_in_container())
```

In the command dispatch, after the `ai-deployment` branch, add:

```python
        elif cmd == 'ai-author':
            _handle_ai_author(mmr, args)
```

Add `'ai-author'` to `_LOCAL_ONLY_COMMANDS` (next to `'ai-deployment'`). Add the handlers after `_withdraw_ai_deployment`:

```python
def _handle_ai_author(mmr: MMR, args: argparse.Namespace):
    action = getattr(args, 'ai_author_action', None)
    if action == 'list':
        print_json_result(mmr.authored_strategies(), title='AI-written strategies (paper)')
        return
    if action != 'prove-isolation':
        print_status('ai-author: choose list or prove-isolation', success=False)
        sys.exit(1)
    _prove_isolation(mmr, args.compose)


def _prove_isolation_in_container() -> int:
    """SP3 D3: run inside research; print the proof body; no key, no trader call."""
    import trader.authored.prove_isolation as prove_isolation
    import trader.messaging.keys_cli as keys_cli

    if not keys_cli.running_in_container():
        print_status('--in-container runs inside the research container only; run it without the flag',
                     success=False)
        sys.exit(2)
    return prove_isolation.run_in_container()


def _prove_isolation(mmr: MMR, compose: str):
    """SP3 spec 7.9 (D3, ruling 15): the in-container step in research, checked here, recorded as cli."""
    import trader.authored.isolation_host as isolation_host
    import trader.messaging.keys_cli as keys_cli
    from trader.messaging.typed_rpc import TypedRpcRemoteError

    if keys_cli.running_in_container():
        print_status('ai-author prove-isolation runs on the host only (it drives docker compose)', success=False)
        sys.exit(2)
    try:
        proof = isolation_host.prove_isolation(isolation_host.compose_command(compose))
    except isolation_host.ProveIsolationError as ex:
        print_status(f'ai-author prove-isolation could not run: {ex}', success=False)
        sys.exit(1)
    try:
        reply = mmr.record_isolation_proof(proof)
    except TypedRpcRemoteError as ex:
        print_status(f'record_isolation_proof rejected: {ex.code}: {ex.message}', success=False)
        sys.exit(1)
    print_json_result(reply, title='Isolation proof (paper)')
    if not proof['passed']:
        print_status('the sandbox image is NOT proven: authored deployments stay refused', success=False)
        sys.exit(1)
```

The handlers read `running_in_container`, `run_in_container` and `prove_isolation` through their modules, so the tests patch them in place.

- [ ] **Step 7: Document the command and the principal**

`docs/CLI_REFERENCE.md`, in the command list after the `ai-deployment withdraw` line, add:

```
ai-author list                               # PAPER AI-written strategies: states, switch, cap, judged count (cli or dashboard)
ai-author prove-isolation                    # operator, host only: isolation checks on the running sandbox build, recorded as cli; exit 1 unless all pass
```

After the `### \`ai-deployment\`` section, add:

```markdown
### `ai-author` (SP3 AI-written strategies)

The `ai` service's `author` role writes at most one strategy file per night. The trader stores it by its
sha256 digest and keeps one record per file: `STORED`, `EVALUATING`, `SHADOW`, `DEPLOYED` (these count toward
`ai_paper.authored_strategies.max_active`, at most 3), or the final `REJECTED`, `RETIRED`, `EXPIRED`, `FAILED`
with a reason (`SANDBOX_ERROR`, `SANDBOX_UNAVAILABLE`, `JEV_REJECT`, `JEV_NO_VERDICT`, `OWNER_RETIRED`,
`SHADOW_EXPIRED`, `DEPLOY_EXPIRED`, `RENEWAL_ENDED`). A `SHADOW` record with reason `AWAITING_REGISTRATION`
was judged DEPLOY and waits for an isolation proof.

`list` (typed `list_authored_strategies`) prints `{"enabled", "max_active", "counted", "room", "judged_count",
"bar_sizes", "records"}`.

`prove-isolation` runs on the host only. It runs `mmr ai-author prove-isolation --in-container` inside the
`research` container (`docker compose exec -T research`, the research socket and pool). That step runs the
sandbox checks: the supervisor's posture from `PONG` (capabilities SETUID/SETGID/KILL only, seccomp, no-new-privileges,
network none, read-only root, pids limit 64, no provider variables), a benign session and every hostile escape. It
prints one JSON proof with the build id the sandbox reports. The host checks the JSON and records it with typed
`record_isolation_proof` as `cli`. Until the newest proof of the build id that research last reported passed, the
trader refuses an authored registration with `AUTHORED_ISOLATION_UNPROVEN`; the strategy service also refuses to load
an authored deployment unless the build id its own `PING` sees has a passing proof. A rebuilt image needs a new run.
`--compose "podman compose"` uses another compose command.
```

In the "Requires trader typed RPC" list, after the `ai-deployment` bullet, add:

```
- `ai-author list|prove-isolation` (SP3 authored strategies; paper only; `prove-isolation` runs on the host and signs as `cli`)
```

`AGENTS.md`: in the typed-RPC paragraph, change the principal list to `` (`trader`, `strategy`, `cli`, `dashboard`, `ai_supervisor`, `ai_research`, `ai_author`, `research`) ``; in the **ai** bullet, change ``signs as `ai_supervisor` and `ai_research`;`` to ``signs as `ai_supervisor`, `ai_research` and `ai_author` (SP3: only `submit_authored_strategy` and `get_authored_strategies`);``.

- [ ] **Step 8: Run the tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/authored/test_prove_isolation.py tests/test_ai_author_cli.py -q --timeout=60`
Expected: PASS. `tests/fullstack/test_prove_isolation_in_research.py` runs only under `scripts/run_sandbox_fullstack.sh` (Docker; the owner runs it). Say in the PR whether it ran.

- [ ] **Step 9: Commit**

```bash
git add trader/authored/prove_isolation.py trader/authored/isolation_host.py trader/sdk.py trader/mmr_cli.py \
  docs/CLI_REFERENCE.md AGENTS.md tests/authored/test_prove_isolation.py tests/test_ai_author_cli.py \
  tests/fullstack/test_prove_isolation_in_research.py
git commit -m "feat: add mmr ai-author list and prove-isolation

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 11: Full suite

**Files:** none.

- [ ] **Step 1: Run the full suite under the shared lock**

```bash
until mkdir /private/tmp/mmr-suite.lock 2>/dev/null; do sleep 30; done
if .venv/bin/python -c "import xdist" 2>/dev/null; then
  .venv/bin/python -m pytest tests/ -q -n 8 --timeout=120 --timeout-method=thread \
    --ignore=tests/test_ibrx_async.py --basetemp=/private/tmp/mmr-suite-sp3-03
else
  .venv/bin/python -m pytest tests/ -q --timeout=60 --timeout-method=thread \
    --ignore=tests/test_ibrx_async.py --basetemp=/private/tmp/mmr-suite-sp3-03
fi
status=$?
.venv/bin/python -m pytest tests/test_ibrx_async.py --timeout=30 -q
rmdir /private/tmp/mmr-suite.lock
exit $status
```

Expected: PASS. Quote the live pytest summary line in the PR, not a remembered count. A new failure in a test that pins exact principal rights, compose key files, `RESTART_ON_ROTATE` or `service_rpc_files("ai")` is fixed in the task that caused it, never by loosening an assertion to a subset.

- [ ] **Step 2: Check the store volume and key mounts statically once more**

Run: `grep -n "ai_author\|mmr_authored_strategies" docker-compose.yml`
Expected: `ai_author.key` only in the `ai` service, `ai_author.pub` in `ai` and `trader`, `mmr_authored_strategies` in `trader` (read-write), in `sandbox` (read-only, from Plan 2 if merged) and once under `volumes:`.

- [ ] **Step 3: Add the rulings to the index**

In `docs/superpowers/plans/2026-10-11-ai-paper-sp3-00-index.md`, under "Owner rulings to confirm", add the 23 lines of this plan's Rulings section, each as `Plan 3 ruling K: ...`. Commit:

```bash
git add docs/superpowers/plans/2026-10-11-ai-paper-sp3-00-index.md
git commit -m "docs: list the plan 3 rulings for the owner

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

No container restart, Docker run, push or GitHub post is part of this task. Before deploying, the operator runs `./docker.sh -k` (ruling 23) and, after the stack is up with Plan 2's sandbox, `mmr ai-author prove-isolation`.
