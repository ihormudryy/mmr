# AI Paper SP2c — Plan 1: Trader: evaluation claims, judgments, limits and cooldown — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** The trader owns the evaluation slots and Jev's verdicts. The `research` principal claims a slot per evaluation (one per canonical request, at most `evaluations_per_day` per New York day, never while the strategy key cools down), reads it back after a lost reply and moves it forward. `ai_research` records one judgment per signed evaluation case; the trader verifies the case itself, offers DEPLOY only for a case that passed every rule (rules first), and starts the strategy key's cooldown on REJECT. The limits live in the operator-only config block `ai_paper.backtest_judge`.

**Architecture:** Two journal tables (migration 110 `evaluation_claims`, 111 `backtest_judgments`) behind two small stores in `trader/automation/` (`EvaluationClaims`, `BacktestJudgments`). Each write is one `DuckDBConnection.transaction`; the per-file lock serialises concurrent claims, so the last slot goes to exactly one caller. The canonical request body and the signed evaluation case are shared formats in `trader/research/` (Plan 3 builds and signs cases; the trader only verifies them against `keys/verify/*.pem`). Six direct typed-RPC handlers in `trader/messaging/backtest_judge_surface.py` check the caller again themselves and answer business refusals as reply bodies. Renewal checks and the forward-evidence read sit behind ports whose Plan 1 defaults refuse, because deployment versions (Plan 2) and shadow rows (Plan 3) do not exist yet; SP2c Plan 5 (Renewal) supplies the real ports.

**Tech Stack:** Python 3.12, DuckDB (`SchemaMigrator`, `DuckDBConnection.transaction`), pydantic v2 strict wire models, Ed25519 (`trader.research.signing`), `exchange_calendars` through `XNYSCalendarPolicy`, pytest. No new dependencies.

**Spec:** `docs/superpowers/specs/2026-10-08-ai-paper-sp2c-backtest-judge-design.md`, sections 3, 5.1 (method table; `submit_evaluation` steps 3, 5, 6, 8; evaluation case), 5.2 items 1–3 and 7, 6.1, 8, 9. Index: `docs/superpowers/plans/2026-10-08-ai-paper-sp2c-00-index.md`.

## Global Constraints

- **Base:** master at `727d56e2` or later. Code read at `/Users/mudryy/private/mmr`.
- **Journal migrations:** this plan uses **110** (`evaluation_claims`) and **111** (`backtest_judgments`). 112–114 stay free. No ALTER, no backfill (owner: no legacy data).
- **Words** from spec section 3, exactly: strategy key (`strategies/<file>.py:<Class>`), evaluation, parameter trial, judgment, evaluation case.
- **Principals:** `claim_evaluation`, `update_evaluation_claim` (command) and `get_evaluation_claim`, `get_deployment_forward_evidence` (query) are `research` only. `record_backtest_judgment` (command) is `ai_research` only. `get_backtest_judgment` (query) is `research`, `ai_research`, `cli`, `dashboard`. Every handler checks `caller.principal` itself.
- **Wire models:** `ConfigDict(extra="forbid", strict=True, frozen=True)`. Every key is required; optional values are sent as explicit `null`. Times are ISO-8601 text with an offset; naive times are refused. `True` is never an int.
- **Refusals:** a business refusal is a reply body `{"status": "REFUSED", "code": ..., "detail": ..., "retryable": ...}`, never an RPC error. A wrong caller is `PERMISSION_DENIED`; a malformed body is `VALIDATION_ERROR`; a tampered judgment row is the RPC error `JUDGMENT_TAMPERED` (fail loudly).
- **No controller epoch, no command ledger** on these methods. They record facts; idempotency rests on the request id, the judgment id and body digests.
- **DuckDB:** only `DuckDBConnection.execute` / `.transaction`. Inside a transaction callback never call `db.execute` or `db.transaction` (non-reentrant lock). File I/O, signature checks and port calls run before the transaction.
- **Config:** `ai_paper.backtest_judge` in `trader.yaml` only. `AI_PAPER*` env names are already refused by `_refuse_env_overrides`. No RPC method writes it.
- **Paper only:** the surface registers only when `ai_paper.enabled` built the services, and that build refuses a live account.
- **Never print secrets.** Key errors name the file and the exception class only.
- Test-first. Per task run only the listed tests. Full suite once, in Task 7.
- Commit subjects `feat:` / `test:` / `chore:`, lowercase, imperative. Every commit ends with a blank line and `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.
- No container restart, broker order, deploy, push or GitHub post is authorized by this plan.

## Rulings

1. **Plan 1 owns the case format; Plan 3 owns its content.** The trader must verify the case before Plan 3 exists, so the model, the file envelope, the digest rule and the verifier are defined here (`trader/research/evaluation_case.py`). The trader reads only the typed header fields; Plan 3 puts its detailed evidence (per-point rule results, cost stress, deflated Sharpe, `strategy_trials` count, `previously_revealed` sessions, warm-up) in the signed `evidence` object. *Cost if wrong:* a header field Plan 3 needs to add is a one-line model change plus a fixture update.
2. **Request id.** `"sha256:" + sha256(b"mmr.research.evaluation-request.v1\n" + canonical body)`. The body carries `research_day` (the controller's cycle day) so the same candidate can be evaluated again on another day. The trader first looks the id up: the same id with the same canonical body returns the claim (`EXISTING`); with another body it is `EVALUATION_REQUEST_CONFLICT` (spec 5.1 step 3). Only for a new id does it recompute the digest and refuse a caller-chosen id (`EVALUATION_REQUEST_ID_MISMATCH`). *Cost if wrong:* without `research_day` a candidate could never be re-evaluated after its first request.
3. **One spelling per request.** The wire model refuses unsorted or repeated conids and repeated cohort points, and allows only scalar tunable values with upper-case names. The signed case keeps the same rule (`check_cohort`): no repeated cohort point, and an INITIAL case that names `selected_params` must pick one of its cohort points (canonical bytes, so `15` and `15.0` are different points); otherwise the trader could bind a DEPLOY to params it never claimed. The research service and the trader then hash identical bytes. *Cost if wrong:* a list-valued tunable needs a model change.
4. **New York day and the cap.** The claim day is the America/New_York calendar date of the trader's clock at claim time (not an XNYS session, not the body's `research_day`). The cap counts every claim of that day whatever its state: a `FAILED` claim still counts (spec 8). *Cost if wrong:* a failed evaluation could be retried without limit on the same day.
5. **Defence in depth at claim.** The trader also refuses a strategy key off `strategy_allowlist` (`STRATEGY_NOT_ALLOWED`) and a cohort larger than `max_cohort_points` (`COHORT_TOO_LARGE`). Both checks run after the existing-id check, so a retry of an accepted claim still returns it after a config change. *Cost if wrong:* a research-service bug could spend slots on strategies the operator never listed.
6. **Forward-only states.** Rank `QUEUED (0) < RUNNING (1) < DONE = FAILED (2)`. A move to a higher rank is `UPDATED`; the same state is `UNCHANGED`; anything else (back, or `DONE`↔`FAILED`) is `CLAIM_STATE_BACKWARD`. `QUEUED → DONE` is allowed (a lost `RUNNING` update must not block the end). *Cost if wrong:* a stricter chain would strand a claim whose `RUNNING` reply was lost.
7. **Cooldown on the judgment row.** A REJECT (INITIAL or RENEWAL) stores `cooldown_until_session` = the N-th XNYS session strictly after the trader's New York day at recording, N = `family_cooldown_sessions` at that moment. A strategy key cools down while today's New York day ≤ the latest such session. One query, no second table, and a later config edit never shortens a running cooldown. *Cost if wrong:* the caller's `decided_at` cannot backdate a cooldown; a judgment recorded just after midnight starts one day later.
8. **Judgment checks.** The trader loads and verifies the case from `artifacts/cases/`, then requires: judgment kind = case kind; `decided_at` not more than 5 minutes ahead of the trader clock; `menu` exactly equal to the menu the case offers (`["DEPLOY","SHADOW","REJECT"]` only for a qualifying case, else `["SHADOW","REJECT"]`); for INITIAL a claim that is `DONE` or `FAILED`, has the same strategy key, cohort, conids, bar size and day, and was claimed before `decided_at`. One judgment per case and one per evaluation (request id): same id and same body → `EXISTING`; same id with another body, or another id for the same case or evaluation → `JUDGMENT_CONFLICT`. The body digest normalises `decided_at` to UTC, so a respelled retry is `EXISTING`. *Cost if wrong:* without the claim match a stray signed case could be judged outside any counted evaluation.
9. **Rules first is structural.** "Qualifying" for INITIAL means: stage `COMPLETE`, `holdout_passed is True`, `decision_state == "PAPER_ELIGIBLE"`, `ruleset_digest == PAPER_V1.digest`, and exactly the paper-v1 rule codes, all passed (the check `paper_materials.py` already uses). A DEPLOY on anything else fails the menu check; `DEPLOY_NOT_ALLOWED` stays as a second guard. *Cost if wrong:* a looser check would let a controller bug offer DEPLOY to a failing case.
10. **Judgments carry `jev_model`.** Spec 5.1 `attest_from_judgment` writes `reviewer = <Jev model id>#<judgment id>` from the trader's record, so the body needs the model id. Added to the spec's body list. *Cost if wrong:* Plan 3 would have to trust the caller for the reviewer name.
11. **Renewal and forward evidence wait for Plan 5 (Renewal).** `record_backtest_judgment` asks a `RenewalChecks` port about a RENEWAL case; the Plan 1 default refuses every RENEWAL with `RENEWAL_NOT_SUPPORTED`. `get_deployment_forward_evidence` is registered with its ACL row, wire model and handler over a `ForwardEvidenceSource` port; the Plan 1 default refuses with `DEPLOYMENT_VERSION_UNKNOWN`. Plan 5 supplies both real ports (over Plan 2's versions, paper trips and bundle expiry, and Plan 3's `shadow_results` rows). *Cost if wrong:* if the owner wants these inside Plan 5 entirely, delete the two ports and one handler here.
12. **Minimal `research` principal.** Plan 1 adds `research` only as a caller of the trader (see Cross-plan additions). The trader container must then bind `research.pub`, so **the operator runs `./docker.sh -k` before deploying this plan** (`docker.sh` refuses to start while a bound key file is missing). *Cost if wrong:* none in code; the deploy fails loudly until the key exists.
13. **Judgments are sealed.** Each row stores a record digest over all its fields (domain `mmr.backtest-judgment.v1`); every read recomputes it. An edited row reads as `JUDGMENT_TAMPERED`, never as missing. *Cost if wrong:* an edited verdict could feed Plan 2's registration unnoticed.
14. **Case keys.** Any `*.pem` in `~/.config/mmr/keys/verify/` is trusted (spec 5.2 item 2), loaded through `load_verify_key`, which refuses an RPC identity key. Keys load on every `record` call (rotation needs no restart). One unreadable `.pem` refuses the call (`CASE_VERIFY_KEYS_UNREADABLE`); no keys → `CASE_VERIFY_KEYS_MISSING`. A symlinked case file is `CASE_NOT_FOUND`. *Cost if wrong:* one broken `.pem` stops all judgments until the operator fixes it (the loud, safe side).
15. **The holdout result is a typed header field** (PR #91 thread 4218218927). `holdout_passed: bool | None` is set by the code that ran the holdout and is bound to the stage by the model: INITIAL `COMPLETE` needs `True`, `HOLDOUT_FAILED` needs `False`, `PRE_HOLDOUT_FAILED` and `FAILED` need `None`; every RENEWAL needs `None`. So a signed case that says `COMPLETE`, `PAPER_ELIGIBLE` and all rules passed but whose holdout failed cannot load (`CASE_MALFORMED`), and `initial_deploy_allowed` checks the field again. The trader no longer has to trust that the stage string and the holdout agree. The signed `evidence` is bound to the header too (PR #91 OpenAI round 2): an INITIAL case must carry the key `evidence["holdout"]`; for `COMPLETE` and `HOLDOUT_FAILED` it is a dict whose `passed` is a real bool (`type(x) is bool`) equal to `holdout_passed`, for `PRE_HOLDOUT_FAILED` and `FAILED` it is `null`; a RENEWAL case has it absent or `null`. So a case whose header says the holdout passed while its evidence says it failed is `CASE_MALFORMED` and never reaches the menu. *Cost if wrong:* none; Plan 3 already knows the holdout result when it builds the case, and writes the header and the evidence from that one result.

## Cross-plan additions

Plans 2, 3 and 4 use these exact names.

**Principals (`trader/messaging/principals.py`). Plan 1 adds exactly:**
- `KNOWN_PRINCIPALS` gains `"research"`; `CALLS["research"] = frozenset({"trader"})`; `SERVER_ACCEPTS["trader"]` gains `"research"`.
- `TRADER_ACL`: `("command", "claim_evaluation")`, `("query", "get_evaluation_claim")`, `("command", "update_evaluation_claim")`, `("query", "get_deployment_forward_evidence")` → `{"research"}`; `("command", "record_backtest_judgment")` → `{"ai_research"}`; `("query", "get_backtest_judgment")` → `{"research", "ai_research", "cli", "dashboard"}`.
- `docker-compose.yml`: the `trader` service binds `${HOME}/.config/mmr/keys/rpc/research.pub` read-only.
- So `RESTART_ON_ROTATE["research"] == ("trader",)` after Plan 1.
- **Not added (Plan 3 adds them):** `SERVER_PRINCIPALS`, `SERVER_ACCEPTS["research"]`, `CALLS["ai_research"]` / `CALLS["cli"]` gaining `research`, `SERVICE_PRINCIPAL["research"]`, `_LONG_LIVED_SERVICE_PRINCIPALS["research"]`, `KEYCHECK_SERVICES`, the `ai` container's `research.pub`, ports 42106/42107 and their rows in the AGENTS.md / ARCHITECTURE.md port tables (Plan 1 only adds `research` to the principal lists). Plan 3 updates `tests/test_research_principal.py` (`RESTART_ON_ROTATE["research"]` becomes `("ai", "research", "trader")`; `record_shadow_result` joins the `research` rights).

**Strategy key (`trader/research/strategy_key.py`):** `STRATEGY_KEY` (regex `^strategies/[A-Za-z0-9_]+(/[A-Za-z0-9_]+)*\.py:[A-Za-z_][A-Za-z0-9_]{0,63}$`), `is_strategy_key(value) -> bool`, `split_strategy_key(key) -> tuple[str, str]` (path, class).

**Config (`trader/automation/backtest_judge_config.py`):** `BacktestJudgeConfig(evaluations_per_day=10, family_cooldown_sessions=10, max_active_deploys=3, deploy_expiry_sessions=20, max_cohort_points=3, shadow_warmup_sessions=5, strategy_allowlist: tuple[str, ...] = ())` (`family_cooldown_sessions` and `deploy_expiry_sessions` are 1-120, `MAX_SESSION_COUNT`), `.allows(strategy_key) -> bool`; `load_backtest_judge_config(raw: object) -> BacktestJudgeConfig` (pure; the research service parses its read-only `trader.yaml` with it); `AiPaperConfig.backtest_judge`.

**Evaluation request (`trader/research/evaluation_request.py`):** `EvaluationRequestBody` = `{strategy_key: str, cohort: list[dict[str, bool|int|float|str]] (1-10 points, distinct, UPPER_CASE names), conids: list[int] (1-20, strictly increasing), bar_size: str (≤ 15 mins), research_day: str (YYYY-MM-DD)}`; `evaluation_request_id(body) -> "sha256:<hex>"`; `canonical_request_json(body) -> str`; `EVALUATION_REQUEST_DOMAIN = "mmr.research.evaluation-request.v1"`, `REQUEST_ID`, `MAX_COHORT_POINTS_LIMIT = 10`, `check_params`, `check_cohort(points)` (valid and distinct by canonical bytes), `is_cohort_point(params, cohort) -> bool`, `check_conids`, `check_bar_size`. Plan 3's `submit_evaluation` takes this body.

**Evaluation case (`trader/research/evaluation_case.py`):**
- `EvaluationCase` fields: `schema_version: "mmr.research.evaluation-case.v1"`, `kind: "INITIAL"|"RENEWAL"`, `request_id: str|None` (INITIAL only), `claim_day: str|None` (INITIAL only), `strategy_key`, `strategy_file_hash: "sha256:<hex>"`, `cohort` (RENEWAL: exactly `[selected_params]`), `conids`, `bar_size`, `stage: "PRE_HOLDOUT_FAILED"|"HOLDOUT_FAILED"|"COMPLETE"|"FAILED"` (INITIAL) or `"FORWARD_COMPLETE"|"FORWARD_INCOMPLETE"` (RENEWAL), `holdout_passed: bool|None` (ruling 15: `True` exactly for `COMPLETE`, `False` exactly for `HOLDOUT_FAILED`, `null` for `PRE_HOLDOUT_FAILED`, `FAILED` and every RENEWAL; Plan 3 sets it from its holdout outcome, Plan 5's renewal case sets `null`), `selected_params`, `family_id`, `selected_trial_id`, `artifact_id`, `eligibility_decision_digest`, `decision_state`, `ruleset_digest` (all `str|None`; required for `COMPLETE` and `HOLDOUT_FAILED`; `artifact_id` null for `PRE_HOLDOUT_FAILED`), `final_rule_results: list[{code: str, passed: bool}]` (the selected point's full paper-v1 decision; empty without a holdout), `renewal: {prior_deployment_version: "sha256:<hex>", forward_sessions: int, incomplete_sessions: int}|None`, `created_at: str` (aware), `evidence: dict` (Plan 3's detail, signed; the trader reads only `evidence["holdout"]`, bound to the header by ruling 15: INITIAL `COMPLETE`/`HOLDOUT_FAILED` → `{start, end, passed: bool == holdout_passed, detail}`, INITIAL `PRE_HOLDOUT_FAILED`/`FAILED` → `null` (key present), RENEWAL → absent or `null`). An INITIAL `selected_params`, when set, is one of the `cohort` points; `cohort` never repeats a point.
- File `artifacts/cases/sha256_<hex>.json` holds the canonical JSON of `{"case": <body>, "case_digest": "sha256:<hex>", "public_key_id": "ed25519-...", "signature": "<urlsafe b64>"}`. Digest and signature cover the same bytes: `b"mmr.research.evaluation-case.v1\n" + canonical_json_bytes(body)`.
- Functions: `case_digest(body) -> str`, `case_path(cases_dir, digest) -> Path`, `write_evaluation_case(cases_dir, case, signer: AttestationSigner) -> str`, `load_case_verify_keys(verify_dir) -> dict[str, Ed25519PublicKey]`, `load_verified_case(cases_dir, digest, keys) -> EvaluationCase`, `initial_deploy_allowed(case)`, `renewal_forward_complete(case)`, `offered_menu(case) -> tuple[str, ...]`, `FULL_MENU = ("DEPLOY","SHADOW","REJECT")`, `NO_DEPLOY_MENU = ("SHADOW","REJECT")`, `default_cases_dir()`, `default_verify_dir()`. `CaseRefused(code, detail)` codes: `CASE_DIGEST_INVALID`, `CASE_NOT_FOUND`, `CASE_MALFORMED`, `CASE_DIGEST_MISMATCH`, `CASE_KEY_UNKNOWN`, `CASE_SIGNATURE_INVALID`, `CASE_VERIFY_KEYS_MISSING`, `CASE_VERIFY_KEYS_UNREADABLE`, `CASE_EXISTS_DIFFERENT`.

**RPC (`trader/automation/backtest_judge_wire.py`):**
- `claim_evaluation` `{request_id, body: EvaluationRequestBody}` → `{"status": "ACCEPTED"|"EXISTING"|"REFUSED", "code", "detail", "retryable": bool, "claim": ClaimView|null}`. `ClaimView` = `{request_id, strategy_key, ny_day: "YYYY-MM-DD", state, claimed_at, updated_at, body}`. Codes: `EVALUATION_REQUEST_CONFLICT`, `EVALUATION_REQUEST_ID_MISMATCH`, `STRATEGY_NOT_ALLOWED`, `COHORT_TOO_LARGE`, `FAMILY_COOLING_DOWN`, `EVALUATION_LIMIT_REACHED` (none retryable).
- `get_evaluation_claim` `{request_id}` → `{"found": bool, "claim": ClaimView|null}`.
- `update_evaluation_claim` `{request_id, state: "RUNNING"|"DONE"|"FAILED"}` → `{"status": "UPDATED"|"UNCHANGED"|"REFUSED", "code", "detail", "retryable", "claim"}`. Codes: `CLAIM_UNKNOWN`, `CLAIM_STATE_BACKWARD`.
- `record_backtest_judgment` `{judgment_id (^[A-Za-z0-9_-]{8,96}$), case_digest, kind, renewal_of_version: sha256|null (RENEWAL only), verdict: "DEPLOY"|"SHADOW"|"REJECT"|"NO_VERDICT", menu (exactly FULL_MENU or NO_DEPLOY_MENU, in that order), jev_model (^[A-Za-z0-9_./:@+-]{1,128}$), jev_attempt_ref: (^[A-Za-z0-9_.:-]{1,128}$)|null (null only when no model call was sent, so only with `NO_VERDICT`), decided_at, narrative: DeployNarrative|null (DEPLOY only)}` → `{"status": "RECORDED"|"EXISTING"|"REFUSED", "judgment_id", "code", "detail", "retryable", "verdict", "cooldown_until_session": "YYYY-MM-DD"|null}`. `DeployNarrative` = the eight §8.5 fields of `OperatorReview` (`economic_rationale`, `edge_survives_costs`, `known_failure_regimes`, `data_and_survivorship_limits`, `parameter_sensitivity`, `operational_dependencies`, `capacity_and_decay`, `episode_dominance`), each 1–4000 characters, not blank. Codes: the `CaseRefused` codes, `JUDGMENT_KIND_MISMATCH`, `DECIDED_IN_FUTURE`, `JUDGMENT_MENU_MISMATCH`, `DEPLOY_NOT_ALLOWED`, `RENEWAL_VERSION_MISMATCH`, the renewal port's codes (`RENEWAL_NOT_SUPPORTED` in Plan 1), `CASE_CLAIM_UNKNOWN`, `CASE_CLAIM_NOT_FINISHED` (retryable), `CASE_CLAIM_MISMATCH`, `DECIDED_BEFORE_CLAIM`, `JUDGMENT_CONFLICT`.
- `get_backtest_judgment` `{judgment_id: str|null, case_digest: sha256|null}` (exactly one set; one judgment per case, so a lookup by case is unique; Plan 3's shadow discovery reads by case) → `{"found": bool, "judgment": {judgment_id, case_digest, request_id, kind, verdict, strategy_key, body, binding, cooldown_until_session, recorded_at}|null}`. `body` is the request with `decided_at` in UTC. `binding` = `{case_digest, kind, stage, request_id, strategy_key, strategy_path, class_name, strategy_file_hash, params, conids, bar_size, family_id, selected_trial_id, artifact_id, eligibility_decision_digest, prior_deployment_version}` copied from the verified case.
- `get_deployment_forward_evidence` `{deployment_version}` → `{"status": "FOUND"|"REFUSED", "code", "detail", "evidence": dict|null}`.

**Trader internals for Plan 2:** `AiPaperServices.claims` (`EvaluationClaims`), `.judgments` (`BacktestJudgments`), `.forward_evidence` (`ForwardEvidenceSource`). `BacktestJudgments(db, *, config, calendar, cases_dir, verify_dir, now, renewals: RenewalChecks | None = None)`, `.get(judgment_id) -> BacktestJudgment | None` and `.get_by_case(case_digest) -> BacktestJudgment | None` (both raise `JudgmentRefused("JUDGMENT_TAMPERED")`). Port `RenewalChecks.status(case: EvaluationCase, *, now: datetime) -> RenewalStatus(refusal_code: str|None, deploy_block_code: str|None, detail: str)`; Plan 5 passes its implementation where `BacktestJudgments` is built (`_build_ai_paper_parts` after Plan 2 moves it there). Port `ForwardEvidenceSource.read(deployment_version: str) -> dict`, raising `ForwardEvidenceRefused(code, detail)`. `cooling_until_in_tx(conn, strategy_key, today) -> date|None` and `nth_session_after(calendar, day, n) -> date` (refuses `COOLDOWN_CALENDAR_UNAVAILABLE` rather than leaking `DateOutOfBounds`; Plan 2's admission recheck; Plan 3's REJECT shadow window = cooldown plus 10 sessions).

## Review Focus

1. **Two concurrent claims for the last slot of a New York day** must give exactly one `ACCEPTED` and one `EVALUATION_LIMIT_REACHED`, and the row count must equal the cap. → Task 3 `test_two_concurrent_claims_for_the_last_slot_accept_exactly_one`; Task 6 `test_two_concurrent_claims_for_the_last_slot_over_rpc_accept_exactly_one`.
2. **A claim reply lost after the trader committed** must read back as the same `QUEUED` claim, and the retry must return `EXISTING` without a second slot, even when the cap is full. → Task 6 `test_a_lost_reply_after_acceptance_is_read_back_and_never_takes_a_second_slot`; Task 3 `test_a_retry_with_the_same_body_returns_the_claim_and_takes_no_slot`.
3. **A judgment retry that only respells `decided_at`** (`Z`, `+00:00`, `-04:00`) must be `EXISTING`; another body under the same id, or another id for the same case or evaluation, must be `JUDGMENT_CONFLICT`. → Task 4 `test_a_retry_that_only_respells_the_decided_time_is_the_same_judgment`, `test_one_judgment_per_case_and_per_evaluation`.
4. **A DEPLOY on a rule-failing, holdout-failed, tampered, foreign-signed or missing case** must be refused with nothing written and no cooldown. A signed case that claims `COMPLETE` while its signed holdout evidence failed must not load, nor one whose `selected_params` was never claimed. → Task 4 `test_rules_first_a_rule_failing_case_is_never_deployed`, `test_a_tampered_foreign_or_missing_case_records_nothing`, `test_a_signed_complete_case_whose_holdout_failed_is_never_deployed`.
5. **A caller outside a method's row** must get `PERMISSION_DENIED` at the real signed server, and also from the handler behind an open allow-list; `research` holds no other trader right. → Task 6 `test_every_method_is_refused_for_every_caller_outside_its_row`, `test_each_handler_refuses_a_wrong_caller_even_behind_an_open_allow_list`; Task 5 `test_research_has_no_trading_policy_or_decision_right`.

## File map

| File | Responsibility | Task |
|---|---|---|
| `trader/research/strategy_key.py`, `trader/automation/backtest_judge_config.py`, `trader/automation/ai_paper_config.py`, `config_defaults/trader.yaml` | strategy key, config block | 1 |
| `trader/research/evaluation_request.py`, `trader/research/evaluation_case.py` | canonical request, signed case | 2 |
| `trader/automation/backtest_judge_schema.py`, `trader/automation/evaluation_claims.py`, `trader/data/schema_migrations.py` (docstring) | tables 110–111, claims | 3 |
| `trader/automation/backtest_judge_wire.py`, `trader/automation/backtest_judgments.py` | judgment body, judgments, cooldown | 4 |
| `trader/messaging/principals.py`, `docker-compose.yml` | `research` principal, ACL rows | 5 |
| `trader/automation/forward_evidence.py`, `trader/messaging/backtest_judge_surface.py`, `trader/messaging/production_api.py`, `trader/trading/command_stack.py` | RPC surface, wiring | 6 |
| — | full suite | 7 |

Shared test helpers: `tests/automation/backtest_judge_fixtures.py` (created in Task 2, extended in Tasks 3 and 4).

---

### Task 1: The strategy key and the `ai_paper.backtest_judge` config

**Files:**
- Create: `trader/research/strategy_key.py`, `trader/automation/backtest_judge_config.py`
- Modify: `trader/automation/ai_paper_config.py`, `config_defaults/trader.yaml`
- Test: `tests/automation/test_backtest_judge_config.py`

**Interfaces:**
- Consumes: `load_ai_paper_config(raw, *, trading_mode)`, `AiPaperConfigError`.
- Produces: `STRATEGY_KEY`, `is_strategy_key(value: object) -> bool`, `split_strategy_key(key: str) -> tuple[str, str]`; `BacktestJudgeConfig`, `BacktestJudgeConfigError`, `load_backtest_judge_config(raw: object) -> BacktestJudgeConfig`; `AiPaperConfig.backtest_judge: BacktestJudgeConfig`.

- [ ] **Step 1: Write the failing tests** (`tests/automation/test_backtest_judge_config.py`)

```python
"""SP2c Plan 1 Task 1: the operator-only ai_paper.backtest_judge block (spec 6.1)."""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from trader.automation.ai_paper_config import AiPaperConfigError, load_ai_paper_config
from trader.automation.backtest_judge_config import BacktestJudgeConfig
from trader.research.strategy_key import split_strategy_key

REPO_ROOT = Path(__file__).resolve().parents[2]
KEY = "strategies/opening_range_breakout.py:OpeningRangeBreakout"


def judge(section):
    return load_ai_paper_config({"backtest_judge": section}, trading_mode="paper").backtest_judge


def test_defaults_are_the_owner_numbers():
    config = load_ai_paper_config(None, trading_mode="paper").backtest_judge
    assert config == BacktestJudgeConfig()
    assert (config.evaluations_per_day, config.family_cooldown_sessions, config.max_active_deploys,
            config.deploy_expiry_sessions, config.max_cohort_points, config.shadow_warmup_sessions,
            config.strategy_allowlist) == (10, 10, 3, 20, 3, 5, ())


def test_a_full_block_loads_and_the_allowlist_is_exact():
    config = judge({"evaluations_per_day": 4, "family_cooldown_sessions": 12, "max_active_deploys": 2,
                    "deploy_expiry_sessions": 30, "max_cohort_points": 5, "shadow_warmup_sessions": 0,
                    "strategy_allowlist": [KEY]})
    assert (config.evaluations_per_day, config.max_cohort_points, config.strategy_allowlist) == (4, 5, (KEY,))
    assert config.allows(KEY) and not config.allows("strategies/opening_range_breakout.py:Other")


@pytest.mark.parametrize("key,value", [
    ("evaluations_per_day", 0), ("evaluations_per_day", 101), ("evaluations_per_day", True),
    ("evaluations_per_day", "10"), ("family_cooldown_sessions", 1.5), ("max_cohort_points", 11),
    ("shadow_warmup_sessions", -1), ("max_active_deploys", None),
    ("family_cooldown_sessions", 121), ("deploy_expiry_sessions", 121),
])
def test_a_bad_number_is_refused_with_its_key_path(key, value):
    with pytest.raises(AiPaperConfigError, match=f"ai_paper.backtest_judge.{key}"):
        judge({key: value})


@pytest.mark.parametrize("allowlist", [
    "strategies/x.py:Foo", ["strategies/x.py"], ["x.py:Foo"], ["strategies/x.py:Foo", "strategies/x.py:Foo"],
    ["strategies/../x.py:Foo"], [7],
])
def test_a_bad_allowlist_is_refused(allowlist):
    with pytest.raises(AiPaperConfigError, match="strategy_allowlist"):
        judge({"strategy_allowlist": allowlist})


def test_an_unknown_key_is_refused():
    with pytest.raises(AiPaperConfigError, match="ai_paper.backtest_judge.evaluations_per_week: unknown key"):
        judge({"evaluations_per_week": 3})


def test_an_environment_override_is_refused(monkeypatch):
    monkeypatch.setenv("AI_PAPER_BACKTEST_JUDGE_EVALUATIONS_PER_DAY", "99")
    with pytest.raises(AiPaperConfigError, match="environment override refused"):
        load_ai_paper_config({}, trading_mode="paper")


def test_the_shipped_trader_yaml_has_the_block_with_an_empty_allowlist():
    raw = yaml.safe_load((REPO_ROOT / "config_defaults" / "trader.yaml").read_text())
    assert raw["ai_paper"]["backtest_judge"]["strategy_allowlist"] == []
    assert load_ai_paper_config(raw["ai_paper"], trading_mode="paper").backtest_judge == BacktestJudgeConfig()


def test_a_strategy_key_splits_into_path_and_class():
    assert split_strategy_key(KEY) == ("strategies/opening_range_breakout.py", "OpeningRangeBreakout")
    with pytest.raises(ValueError):
        split_strategy_key("strategies/opening_range_breakout.py")
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/automation/test_backtest_judge_config.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'trader.automation.backtest_judge_config'`.

- [ ] **Step 3: Write the implementation**

`trader/research/strategy_key.py`:

```python
"""The strategy key (SP2c spec 3): ``strategies/<file>.py:<Class>``.

Cooldown, the trial count and holdout windows use it. It is not the registry's
``family_id``, which changes with every parameter set.
"""
from __future__ import annotations

import re

STRATEGY_KEY = re.compile(r"^strategies/[A-Za-z0-9_]+(/[A-Za-z0-9_]+)*\.py:[A-Za-z_][A-Za-z0-9_]{0,63}$")


def is_strategy_key(value: object) -> bool:
    return isinstance(value, str) and STRATEGY_KEY.fullmatch(value) is not None


def split_strategy_key(key: str) -> tuple[str, str]:
    if not is_strategy_key(key):
        raise ValueError(f"{key!r} is not strategies/<file>.py:<Class>")
    path, class_name = key.split(":", 1)
    return path, class_name
```

`trader/automation/backtest_judge_config.py`:

```python
"""``ai_paper.backtest_judge`` in trader.yaml (SP2c spec 6.1).

Operator only: no environment override (``load_ai_paper_config`` refuses ``AI_PAPER*``
names) and no RPC method writes it. A trader restart applies an edit.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from trader.research.strategy_key import is_strategy_key

MAX_COHORT_POINTS_LIMIT = 10
MAX_SESSION_COUNT = 120     # about six months of XNYS sessions; nth_session_after must reach it
# key: (default, lowest, highest)
_INTEGER_KEYS: Mapping[str, tuple[int, int, int]] = {
    "evaluations_per_day": (10, 1, 100),
    "family_cooldown_sessions": (10, 1, MAX_SESSION_COUNT),
    "max_active_deploys": (3, 1, 20),
    "deploy_expiry_sessions": (20, 1, MAX_SESSION_COUNT),
    "max_cohort_points": (3, 1, MAX_COHORT_POINTS_LIMIT),
    "shadow_warmup_sessions": (5, 0, 60),
}
_KEYS = (*_INTEGER_KEYS, "strategy_allowlist")


class BacktestJudgeConfigError(ValueError):
    """The block is invalid; the message names the key path."""


@dataclass(frozen=True)
class BacktestJudgeConfig:
    evaluations_per_day: int = 10
    family_cooldown_sessions: int = 10
    max_active_deploys: int = 3
    deploy_expiry_sessions: int = 20
    max_cohort_points: int = 3
    shadow_warmup_sessions: int = 5
    strategy_allowlist: tuple[str, ...] = ()

    def allows(self, strategy_key: str) -> bool:
        return strategy_key in self.strategy_allowlist


def load_backtest_judge_config(raw: object) -> BacktestJudgeConfig:
    if raw is None:
        return BacktestJudgeConfig()
    if not isinstance(raw, dict):
        raise BacktestJudgeConfigError("ai_paper.backtest_judge must be a mapping")
    unknown = sorted(set(raw) - set(_KEYS))
    if unknown:
        raise BacktestJudgeConfigError(f"ai_paper.backtest_judge.{unknown[0]}: unknown key")
    integers = {key: _bounded(raw, key, *bounds) for key, bounds in _INTEGER_KEYS.items()}
    return BacktestJudgeConfig(**integers, strategy_allowlist=_allowlist(raw.get("strategy_allowlist", [])))


def _bounded(raw: Mapping[str, Any], key: str, default: int, low: int, high: int) -> int:
    value = raw.get(key, default)
    if type(value) is not int or not low <= value <= high:
        raise BacktestJudgeConfigError(f"ai_paper.backtest_judge.{key} must be an integer from {low} to {high}")
    return value


def _allowlist(value: object) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise BacktestJudgeConfigError("ai_paper.backtest_judge.strategy_allowlist must be a list")
    for item in value:
        if not is_strategy_key(item):
            raise BacktestJudgeConfigError(
                f"ai_paper.backtest_judge.strategy_allowlist: {item!r} is not strategies/<file>.py:<Class>")
    if len(set(value)) != len(value):
        raise BacktestJudgeConfigError("ai_paper.backtest_judge.strategy_allowlist must not repeat a key")
    return tuple(value)
```

`trader/automation/ai_paper_config.py`: add `"backtest_judge"` to `_PARSED_KEYS`; import `BacktestJudgeConfig`, `BacktestJudgeConfigError`, `load_backtest_judge_config`; add the field after `model_budget_usd_per_day`:

```python
    # SP2c Plan 1 (spec 6.1): evaluation and deployment limits; operator only.
    backtest_judge: BacktestJudgeConfig = field(default_factory=BacktestJudgeConfig)
```

pass `backtest_judge=_parse_backtest_judge(raw.get("backtest_judge"))` in `load_ai_paper_config`, and add:

```python
def _parse_backtest_judge(value: object) -> BacktestJudgeConfig:
    try:
        return load_backtest_judge_config(value)
    except BacktestJudgeConfigError as exc:
        raise AiPaperConfigError(str(exc)) from None
```

`config_defaults/trader.yaml`, inside `ai_paper:` after `model_budget_usd_per_day`:

```yaml
  backtest_judge:                    # SP2c: operator only, no env override; restart the trader after an edit
    evaluations_per_day: 10          # evaluation claims per New York day
    family_cooldown_sessions: 10     # XNYS sessions a REJECTed strategy key cools down
    max_active_deploys: 3            # active AI-judged DEPLOYs
    deploy_expiry_sessions: 20       # a DEPLOY expires after this many sessions
    max_cohort_points: 3             # parameter points per evaluation (1-10)
    shadow_warmup_sessions: 5        # bars that only warm up a shadow replay
    strategy_allowlist: []           # strategies/<file>.py:<Class>; empty = nothing may be evaluated
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/automation/test_backtest_judge_config.py tests/automation/test_ai_paper_config.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add trader/research/strategy_key.py trader/automation/backtest_judge_config.py \
  trader/automation/ai_paper_config.py config_defaults/trader.yaml tests/automation/test_backtest_judge_config.py
git commit -m "$(cat <<'EOF'
feat: add the operator-only ai_paper.backtest_judge config

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 2: The canonical evaluation request and the signed evaluation case

**Files:**
- Create: `trader/research/evaluation_request.py`, `trader/research/evaluation_case.py`
- Create: `tests/automation/backtest_judge_fixtures.py`
- Test: `tests/research/test_evaluation_request_and_case.py`

**Interfaces:**
- Consumes: `canonical_json_bytes`, `sha256_digest` (`trader/research/canonical.py`); `AttestationSigner`, `load_verify_key`, `public_key_id`, `verify_bytes`, `BadSignature`, `MalformedKey`, `InvalidKeyType` (`trader/research/signing.py`); `PAPER_V1` (`trader/research/rulesets/paper_v1.py`); `STATE_PAPER_ELIGIBLE` (`trader/research/eligibility.py`); `BarSize`, `LONGEST_BAR_SIZE`; `is_strategy_key` (Task 1).
- Produces: everything listed under "Evaluation request" and "Evaluation case" in Cross-plan additions.

- [ ] **Step 1: Write the test helpers** (`tests/automation/backtest_judge_fixtures.py`)

```python
"""Shared helpers for SP2c Plan 1 tests: request bodies, signed cases, a mutable clock."""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from pathlib import Path

from trader.research.evaluation_case import CASE_DOMAIN, EvaluationCase
from trader.research.evaluation_request import EvaluationRequestBody, evaluation_request_id
from trader.research.rulesets.paper_v1 import PAPER_V1
from trader.research.signing import AttestationSigner

NOW = dt.datetime(2026, 10, 8, 21, 30, tzinfo=dt.timezone.utc)      # Thursday 17:30 ET, after the close
KEY = "strategies/opening_range_breakout.py:OpeningRangeBreakout"
FILE_HASH = "sha256:" + "a" * 64
VERSION = "sha256:" + "b" * 64
ZERO = "sha256:" + "0" * 64


class Clock:
    def __init__(self, now: dt.datetime):
        self.now = now

    def __call__(self) -> dt.datetime:
        return self.now


def request_body(**changes) -> EvaluationRequestBody:
    raw = {"strategy_key": KEY, "cohort": [{"RANGE_MINUTES": 15}, {"RANGE_MINUTES": 30}],
           "conids": [265598, 272093], "bar_size": "5 mins", "research_day": "2026-10-08"}
    raw.update(changes)
    return EvaluationRequestBody.model_validate(raw)


def passing_results() -> list[dict]:
    return [{"code": rule.code, "passed": True} for rule in PAPER_V1.rules]


def failed_results() -> list[dict]:
    results = passing_results()
    results[0]["passed"] = False
    return results


HOLDOUT_BY_STAGE = {"COMPLETE": True, "HOLDOUT_FAILED": False}     # ruling 15; every other stage: None


def holdout_evidence(stage: str) -> dict | None:
    """Plan 3's evidence.holdout: a result once the holdout ran, else null (ruling 15)."""
    passed = HOLDOUT_BY_STAGE.get(stage)
    if passed is None:
        return None
    return {"start": "2025-01-02", "end": "2025-12-31", "passed": passed, "detail": "fixture holdout"}


def case_body(body: EvaluationRequestBody, *, stage: str = "COMPLETE", **changes) -> dict:
    raw = {"schema_version": CASE_DOMAIN, "kind": "INITIAL", "request_id": evaluation_request_id(body),
           "claim_day": "2026-10-08", "strategy_key": body.strategy_key, "strategy_file_hash": FILE_HASH,
           "cohort": [dict(point) for point in body.cohort], "conids": list(body.conids),
           "bar_size": body.bar_size, "stage": stage, "holdout_passed": HOLDOUT_BY_STAGE.get(stage),
           "selected_params": dict(body.cohort[0]),
           "family_id": "fam-1", "selected_trial_id": "trial-1", "artifact_id": "art-1",
           "eligibility_decision_digest": "d" * 64, "decision_state": "PAPER_ELIGIBLE",
           "ruleset_digest": PAPER_V1.digest, "final_rule_results": passing_results(), "renewal": None,
           "created_at": NOW.isoformat(), "evidence": {"note": "fixture", "holdout": holdout_evidence(stage)}}
    if stage == "PRE_HOLDOUT_FAILED":
        raw.update(selected_params=None, selected_trial_id=None, artifact_id=None, eligibility_decision_digest=None,
                   decision_state=None, ruleset_digest=None, final_rule_results=[])
    raw.update(changes)
    return raw


def make_case(body: EvaluationRequestBody | None = None, **changes) -> EvaluationCase:
    return EvaluationCase.model_validate(case_body(body or request_body(), **changes))


def renewal_case_body(**changes) -> dict:
    raw = case_body(request_body(), kind="RENEWAL", stage="FORWARD_COMPLETE", request_id=None, claim_day=None,
                    cohort=[{"RANGE_MINUTES": 15}], selected_params={"RANGE_MINUTES": 15},
                    renewal={"prior_deployment_version": VERSION, "forward_sessions": 20, "incomplete_sessions": 0})
    raw.update(changes)
    return raw


@dataclass
class CaseKeys:
    signer: AttestationSigner
    verify_dir: Path
    cases_dir: Path


def case_keys(tmp_path: Path) -> CaseKeys:
    signer = AttestationSigner.generate()
    verify_dir = tmp_path / "verify"
    verify_dir.mkdir()
    (verify_dir / "research.pem").write_bytes(signer.public_key_pem())
    return CaseKeys(signer, verify_dir, tmp_path / "artifacts" / "cases")
```

- [ ] **Step 2: Write the failing tests** (`tests/research/test_evaluation_request_and_case.py`)

```python
"""SP2c Plan 1 Task 2: the canonical evaluation request and the signed evaluation case (spec 5.1)."""
from __future__ import annotations

import json
import os

import pytest
from pydantic import ValidationError

from tests.automation.backtest_judge_fixtures import (
    ZERO, case_body, case_keys, failed_results, make_case, renewal_case_body, request_body,
)
from trader.research.evaluation_case import (
    FULL_MENU, NO_DEPLOY_MENU, CaseRefused, EvaluationCase, case_path, initial_deploy_allowed,
    load_case_verify_keys, load_verified_case, offered_menu, write_evaluation_case,
)
from trader.research.evaluation_request import evaluation_request_id
from trader.research.signing import AttestationSigner


def refusal(fn) -> str:
    with pytest.raises(CaseRefused) as exc:
        fn()
    return exc.value.code


def test_the_request_id_is_the_digest_of_the_canonical_body():
    body = request_body()
    assert evaluation_request_id(body) == evaluation_request_id(request_body())
    assert evaluation_request_id(body).startswith("sha256:") and len(evaluation_request_id(body)) == 71
    assert evaluation_request_id(request_body(research_day="2026-10-09")) != evaluation_request_id(body)
    assert evaluation_request_id(request_body(cohort=[{"B": 1, "A": 2}])) == \
        evaluation_request_id(request_body(cohort=[{"A": 2, "B": 1}]))


@pytest.mark.parametrize("changes", [
    {"conids": [272093, 265598]}, {"conids": [265598, 265598]}, {"conids": [True]}, {"conids": []},
    {"cohort": []}, {"cohort": [{"A": i} for i in range(11)]}, {"cohort": [{"A": 1}, {"A": 1}]},
    {"cohort": [{"range": 1}]}, {"cohort": [{"A": float("nan")}]}, {"cohort": [{"A": [1, 2]}]},
    {"bar_size": "1 hour"}, {"bar_size": "soon"}, {"strategy_key": "strategies/x.py"},
    {"research_day": "08/10/2026"}, {"extra": 1},
])
def test_a_request_with_two_spellings_or_bad_values_is_refused(changes):
    with pytest.raises(ValidationError):
        request_body(**changes)


def test_a_written_case_loads_back_verified(tmp_path):
    keys = case_keys(tmp_path)
    case = make_case()
    digest = write_evaluation_case(keys.cases_dir, case, keys.signer)
    assert case_path(keys.cases_dir, digest).name == f"sha256_{digest[7:]}.json"
    assert load_verified_case(keys.cases_dir, digest, load_case_verify_keys(keys.verify_dir)) == case
    assert write_evaluation_case(keys.cases_dir, case, keys.signer) == digest       # same bytes: a no-op


def test_a_changed_case_body_fails_its_digest(tmp_path):
    keys = case_keys(tmp_path)
    digest = write_evaluation_case(keys.cases_dir, make_case(), keys.signer)
    path = case_path(keys.cases_dir, digest)
    envelope = json.loads(path.read_text())
    envelope["case"]["bar_size"] = "1 min"
    path.write_text(json.dumps(envelope))
    assert refusal(lambda: load_verified_case(keys.cases_dir, digest, load_case_verify_keys(keys.verify_dir))) \
        == "CASE_DIGEST_MISMATCH"


def test_a_case_signed_by_an_unknown_key_is_refused(tmp_path):
    keys = case_keys(tmp_path)
    digest = write_evaluation_case(keys.cases_dir, make_case(), AttestationSigner.generate())
    assert refusal(lambda: load_verified_case(keys.cases_dir, digest, load_case_verify_keys(keys.verify_dir))) \
        == "CASE_KEY_UNKNOWN"


def test_a_foreign_signature_under_a_trusted_key_id_is_refused(tmp_path):
    keys = case_keys(tmp_path)
    digest = write_evaluation_case(keys.cases_dir, make_case(), AttestationSigner.generate())
    path = case_path(keys.cases_dir, digest)
    envelope = json.loads(path.read_text())
    envelope["public_key_id"] = keys.signer.public_key_id
    path.write_text(json.dumps(envelope))
    assert refusal(lambda: load_verified_case(keys.cases_dir, digest, load_case_verify_keys(keys.verify_dir))) \
        == "CASE_SIGNATURE_INVALID"


def test_a_missing_or_symlinked_case_is_not_found(tmp_path):
    keys = case_keys(tmp_path)
    digest = write_evaluation_case(keys.cases_dir, make_case(), keys.signer)
    path = case_path(keys.cases_dir, digest)
    elsewhere = tmp_path / "elsewhere.json"
    os.replace(path, elsewhere)
    path.symlink_to(elsewhere)
    verify = load_case_verify_keys(keys.verify_dir)
    assert refusal(lambda: load_verified_case(keys.cases_dir, digest, verify)) == "CASE_NOT_FOUND"
    assert refusal(lambda: load_verified_case(keys.cases_dir, ZERO, verify)) == "CASE_NOT_FOUND"
    assert refusal(lambda: load_verified_case(keys.cases_dir, "../x", verify)) == "CASE_DIGEST_INVALID"


def test_no_verify_keys_is_a_refusal_not_a_crash(tmp_path):
    assert refusal(lambda: load_case_verify_keys(tmp_path / "nothing")) == "CASE_VERIFY_KEYS_MISSING"
    bad = tmp_path / "bad"
    bad.mkdir()
    (bad / "broken.pem").write_text("not a key")
    assert refusal(lambda: load_case_verify_keys(bad)) == "CASE_VERIFY_KEYS_UNREADABLE"


def test_only_a_complete_case_with_every_paper_v1_rule_passed_offers_deploy():
    assert offered_menu(make_case()) == FULL_MENU
    assert offered_menu(make_case(final_rule_results=failed_results())) == NO_DEPLOY_MENU
    short = case_body(request_body())
    short["final_rule_results"].pop()
    assert offered_menu(EvaluationCase.model_validate(short)) == NO_DEPLOY_MENU
    assert offered_menu(make_case(decision_state="CANDIDATE")) == NO_DEPLOY_MENU
    assert offered_menu(make_case(stage="HOLDOUT_FAILED", decision_state="CANDIDATE")) == NO_DEPLOY_MENU
    assert offered_menu(make_case(stage="PRE_HOLDOUT_FAILED")) == NO_DEPLOY_MENU
    assert offered_menu(EvaluationCase.model_validate(renewal_case_body())) == FULL_MENU
    incomplete = renewal_case_body(renewal={"prior_deployment_version": "sha256:" + "b" * 64,
                                            "forward_sessions": 20, "incomplete_sessions": 1})
    assert offered_menu(EvaluationCase.model_validate(incomplete)) == NO_DEPLOY_MENU


def test_deploy_needs_the_typed_holdout_result_too():                                 # ruling 15
    unchecked = make_case().model_copy(update={"holdout_passed": False})            # skips validation on purpose
    assert initial_deploy_allowed(unchecked) is False and offered_menu(unchecked) == NO_DEPLOY_MENU
    assert make_case().holdout_passed is True
    assert make_case(stage="HOLDOUT_FAILED", decision_state="CANDIDATE").holdout_passed is False
    assert make_case(stage="PRE_HOLDOUT_FAILED").holdout_passed is None
    assert EvaluationCase.model_validate(renewal_case_body()).holdout_passed is None
    with pytest.raises(ValidationError):
        EvaluationCase.model_validate(renewal_case_body(holdout_passed=True))       # a renewal opens no holdout


@pytest.mark.parametrize("changes", [
    {"holdout_passed": False},                                 # COMPLETE needs a passed holdout
    {"holdout_passed": None},
    {"stage": "HOLDOUT_FAILED", "holdout_passed": True},       # HOLDOUT_FAILED needs a failed holdout
    {"stage": "PRE_HOLDOUT_FAILED", "holdout_passed": False},  # no holdout ran: no result
    {"stage": "PRE_HOLDOUT_FAILED", "artifact_id": "art-1"},  # a pre-holdout failure seals no artifact
    {"artifact_id": None},                                     # a sealed case names its artifact
    {"request_id": None},                                      # an INITIAL case names its claim
    {"renewal": {"prior_deployment_version": "sha256:" + "b" * 64, "forward_sessions": 1,
                 "incomplete_sessions": 0}},                   # only a RENEWAL has renewal facts
    {"created_at": "2026-10-08T21:30:00"},                     # naive time
    {"conids": [272093, 265598]},
])
def test_a_case_shape_that_contradicts_its_stage_is_refused(changes):
    with pytest.raises(ValidationError):
        make_case(**changes)


def holdout(passed) -> dict:
    return {"start": "2025-01-02", "end": "2025-12-31", "passed": passed, "detail": "x"}


@pytest.mark.parametrize("stage,evidence", [
    ("COMPLETE", {}),                                       # no holdout key
    ("COMPLETE", {"holdout": None}),
    ("COMPLETE", {"holdout": holdout(1)}),                  # not a real bool
    ("COMPLETE", {"holdout": holdout("true")}),
    ("COMPLETE", {"holdout": {"start": "2025-01-02"}}),      # no passed
    ("COMPLETE", {"holdout": holdout(False)}),              # header True, evidence False
    ("HOLDOUT_FAILED", {"holdout": holdout(True)}),         # header False, evidence True
    ("PRE_HOLDOUT_FAILED", {}),
    ("PRE_HOLDOUT_FAILED", {"holdout": holdout(False)}),    # no holdout ran
    ("FAILED", {"holdout": holdout(True)}),
])
def test_the_holdout_evidence_must_agree_with_the_header(stage, evidence):          # ruling 15
    with pytest.raises(ValidationError, match="holdout"):
        make_case(stage=stage, evidence=evidence)


def test_a_renewal_case_carries_no_holdout_evidence():
    assert EvaluationCase.model_validate(renewal_case_body(evidence={})).kind == "RENEWAL"
    with pytest.raises(ValidationError, match="holdout"):
        EvaluationCase.model_validate(renewal_case_body(evidence={"holdout": holdout(True)}))


@pytest.mark.parametrize("stage", ["COMPLETE", "HOLDOUT_FAILED", "FAILED"])
def test_the_selected_params_must_be_one_of_the_claimed_cohort_points(stage):          # ruling 3
    with pytest.raises(ValidationError, match="cohort"):
        make_case(stage=stage, selected_params={"RANGE_MINUTES": 99})
    with pytest.raises(ValidationError, match="cohort"):
        make_case(stage=stage, selected_params={"RANGE_MINUTES": 15.0})       # another spelling is another point
    assert make_case(stage=stage, selected_params={"RANGE_MINUTES": 30}).selected_params == {"RANGE_MINUTES": 30}


def test_a_case_never_repeats_a_cohort_point():
    with pytest.raises(ValidationError, match="distinct"):
        make_case(cohort=[{"RANGE_MINUTES": 15}, {"RANGE_MINUTES": 15}])
```

- [ ] **Step 3: Run the tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/research/test_evaluation_request_and_case.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'trader.research.evaluation_case'` (the helpers import it first).

- [ ] **Step 4: Write the implementation**

`trader/research/evaluation_request.py`:

```python
"""The canonical evaluation request (SP2c spec 5.1 step 1).

The request id is the digest of the canonical body, so a caller cannot choose it.
The research service and the trader hash the same bytes: the model refuses every
input with two spellings (unsorted or repeated conids, repeated points).
"""
from __future__ import annotations

import datetime as dt
import math
import re
from typing import Union

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictFloat, StrictInt, StrictStr, field_validator

from trader.objects import BarSize
from trader.research.canonical import canonical_json_bytes, sha256_digest
from trader.research.evaluation_spec import LONGEST_BAR_SIZE
from trader.research.strategy_key import is_strategy_key

EVALUATION_REQUEST_DOMAIN = "mmr.research.evaluation-request.v1"
REQUEST_ID = re.compile(r"^sha256:[0-9a-f]{64}$")
TUNABLE_NAME = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")
MAX_COHORT_POINTS_LIMIT = 10
MAX_CONIDS = 20
MAX_PARAMS = 32
ParamValue = Union[StrictBool, StrictInt, StrictFloat, StrictStr]


def check_params(params: dict) -> dict:
    """One parameter point: upper-case tunable names and finite scalar values only."""
    if len(params) > MAX_PARAMS:
        raise ValueError(f"a parameter point has at most {MAX_PARAMS} tunables")
    for name, value in params.items():
        if not TUNABLE_NAME.fullmatch(name):
            raise ValueError(f"{name!r} is not an upper-case tunable name")
        if isinstance(value, float) and not math.isfinite(value):
            raise ValueError(f"{name} must be finite")
        if isinstance(value, str) and not 1 <= len(value) <= 128:
            raise ValueError(f"{name} must be 1-128 characters")
    return params


def check_cohort(points: list[dict]) -> list[dict]:
    """Every point is a valid parameter set and no point repeats (by its canonical spelling)."""
    for point in points:
        check_params(point)
    if len({canonical_json_bytes(point) for point in points}) != len(points):
        raise ValueError("cohort points must be distinct")
    return points


def is_cohort_point(params: dict, cohort: list[dict]) -> bool:
    return canonical_json_bytes(params) in {canonical_json_bytes(point) for point in cohort}


def check_conids(conids: list[int]) -> list[int]:
    if any(conid <= 0 for conid in conids):
        raise ValueError("conids must be positive")
    if any(later <= earlier for earlier, later in zip(conids, conids[1:])):
        raise ValueError("conids must be strictly increasing (sorted, no repeats)")
    return conids


def check_bar_size(bar_size: str) -> str:
    try:
        parsed = BarSize.parse_str(bar_size)
    except ValueError:
        raise ValueError(f"{bar_size!r} is not a bar size") from None
    if parsed > LONGEST_BAR_SIZE:
        raise ValueError(f"{bar_size!r} is longer than 15 minutes")
    return bar_size


class EvaluationRequestBody(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    strategy_key: str
    cohort: list[dict[str, ParamValue]] = Field(min_length=1, max_length=MAX_COHORT_POINTS_LIMIT)
    conids: list[int] = Field(min_length=1, max_length=MAX_CONIDS)
    bar_size: str
    research_day: str

    @field_validator("strategy_key")
    @classmethod
    def _strategy_key(cls, value: str) -> str:
        if not is_strategy_key(value):
            raise ValueError("strategy_key must be strategies/<file>.py:<Class>")
        return value

    @field_validator("cohort")
    @classmethod
    def _cohort(cls, value: list[dict]) -> list[dict]:
        return check_cohort(value)

    @field_validator("conids")
    @classmethod
    def _conids(cls, value: list[int]) -> list[int]:
        return check_conids(value)

    @field_validator("bar_size")
    @classmethod
    def _bar_size(cls, value: str) -> str:
        return check_bar_size(value)

    @field_validator("research_day")
    @classmethod
    def _research_day(cls, value: str) -> str:
        if len(value) != 10:
            raise ValueError("research_day must be YYYY-MM-DD")
        dt.date.fromisoformat(value)
        return value


def canonical_request_json(body: EvaluationRequestBody) -> str:
    return canonical_json_bytes(body.model_dump()).decode("utf-8")


def evaluation_request_id(body: EvaluationRequestBody) -> str:
    return "sha256:" + sha256_digest(EVALUATION_REQUEST_DOMAIN, body.model_dump())
```

`trader/research/evaluation_case.py`:

```python
"""The signed evaluation case (SP2c spec 5.1). Plan 3 builds and signs it; the trader verifies it.

A case never authorizes trading: it has no attestation, review or manifest. A case
file is the canonical JSON of ``{"case", "case_digest", "public_key_id", "signature"}``.
The digest and the signature cover the same bytes: the domain tag, a newline and the
canonical case body.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import re
import uuid
from pathlib import Path
from typing import Any, Literal, Mapping, Optional

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from trader.research.canonical import canonical_json_bytes, sha256_digest
from trader.research.eligibility import STATE_PAPER_ELIGIBLE
from trader.research.evaluation_request import (
    MAX_COHORT_POINTS_LIMIT, REQUEST_ID, ParamValue, check_bar_size, check_cohort, check_conids, check_params,
    is_cohort_point,
)
from trader.research.rulesets.paper_v1 import PAPER_V1
from trader.research.signing import (
    AttestationSigner, BadSignature, InvalidKeyType, MalformedKey, load_verify_key, public_key_id, verify_bytes,
)
from trader.research.strategy_key import is_strategy_key

CASE_DOMAIN = "mmr.research.evaluation-case.v1"
INITIAL_STAGES = ("PRE_HOLDOUT_FAILED", "HOLDOUT_FAILED", "COMPLETE", "FAILED")
HOLDOUT_RESULT_BY_STAGE = {"COMPLETE": True, "HOLDOUT_FAILED": False, "PRE_HOLDOUT_FAILED": None, "FAILED": None}
RENEWAL_STAGES = ("FORWARD_COMPLETE", "FORWARD_INCOMPLETE")
FULL_MENU = ("DEPLOY", "SHADOW", "REJECT")
NO_DEPLOY_MENU = ("SHADOW", "REJECT")
MAX_CASE_BYTES = 4 * 1024 * 1024
PAPER_V1_CODES = frozenset(rule.code for rule in PAPER_V1.rules)
_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_ID = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")
_ENVELOPE_KEYS = frozenset({"case", "case_digest", "public_key_id", "signature"})


class CaseRefused(Exception):
    def __init__(self, code: str, detail: str):
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class RuleOutcome(_Strict):
    code: str
    passed: bool


class RenewalFacts(_Strict):
    prior_deployment_version: str
    forward_sessions: int = Field(ge=0)
    incomplete_sessions: int = Field(ge=0)

    @field_validator("prior_deployment_version")
    @classmethod
    def _version(cls, value: str) -> str:
        if not _DIGEST.fullmatch(value):
            raise ValueError("prior_deployment_version must be sha256:<hex>")
        return value


class EvaluationCase(_Strict):
    schema_version: Literal["mmr.research.evaluation-case.v1"]
    kind: Literal["INITIAL", "RENEWAL"]
    request_id: Optional[str]
    claim_day: Optional[str]
    strategy_key: str
    strategy_file_hash: str
    cohort: list[dict[str, ParamValue]]
    conids: list[int]
    bar_size: str
    stage: Literal["PRE_HOLDOUT_FAILED", "HOLDOUT_FAILED", "COMPLETE", "FAILED",
                   "FORWARD_COMPLETE", "FORWARD_INCOMPLETE"]
    holdout_passed: Optional[bool]
    selected_params: Optional[dict[str, ParamValue]]
    family_id: Optional[str]
    selected_trial_id: Optional[str]
    artifact_id: Optional[str]
    eligibility_decision_digest: Optional[str]
    decision_state: Optional[str]
    ruleset_digest: Optional[str]
    final_rule_results: list[RuleOutcome]
    renewal: Optional[RenewalFacts]
    created_at: str
    evidence: dict[str, Any]

    @model_validator(mode="after")
    def _shape(self) -> "EvaluationCase":
        if not is_strategy_key(self.strategy_key):
            raise ValueError("strategy_key must be strategies/<file>.py:<Class>")
        if not _DIGEST.fullmatch(self.strategy_file_hash):
            raise ValueError("strategy_file_hash must be sha256:<hex>")
        check_cohort(self.cohort)
        if self.selected_params is not None:
            check_params(self.selected_params)
        check_conids(self.conids)
        check_bar_size(self.bar_size)
        created = dt.datetime.fromisoformat(self.created_at)
        if created.tzinfo is None or created.utcoffset() is None:
            raise ValueError("created_at must carry an offset")
        for name in ("family_id", "selected_trial_id", "artifact_id", "eligibility_decision_digest",
                     "decision_state", "ruleset_digest"):
            value = getattr(self, name)
            if value is not None and not _ID.fullmatch(value):
                raise ValueError(f"{name} must match {_ID.pattern}")
        if self.kind == "INITIAL":
            self._initial_shape()
        else:
            self._renewal_shape()
        self._holdout_evidence_shape()
        return self

    def _initial_shape(self) -> None:
        if self.stage not in INITIAL_STAGES or self.renewal is not None:
            raise ValueError("an INITIAL case has an evaluation stage and no renewal facts")
        if self.request_id is None or not REQUEST_ID.fullmatch(self.request_id) or self.claim_day is None:
            raise ValueError("an INITIAL case names its request id and claim day")
        dt.date.fromisoformat(self.claim_day)
        if not 1 <= len(self.cohort) <= MAX_COHORT_POINTS_LIMIT:
            raise ValueError("an INITIAL case holds its whole cohort")
        sealed_fields = (self.selected_params, self.family_id, self.selected_trial_id, self.artifact_id,
                         self.eligibility_decision_digest, self.decision_state, self.ruleset_digest)
        if self.stage in ("COMPLETE", "HOLDOUT_FAILED") and (
                any(value is None for value in sealed_fields) or not self.final_rule_results):
            raise ValueError("a case that opened its holdout names its artifact, decision and rule results")
        if self.selected_params is not None and not is_cohort_point(self.selected_params, self.cohort):
            raise ValueError("selected_params must be one of the claimed cohort points")
        if self.stage == "PRE_HOLDOUT_FAILED" and (self.artifact_id is not None or self.final_rule_results):
            raise ValueError("a pre-holdout failure seals no artifact")
        if self.holdout_passed is not HOLDOUT_RESULT_BY_STAGE[self.stage]:
            raise ValueError(f"stage {self.stage} needs holdout_passed {HOLDOUT_RESULT_BY_STAGE[self.stage]}")

    def _renewal_shape(self) -> None:
        if self.stage not in RENEWAL_STAGES or self.renewal is None:
            raise ValueError("a RENEWAL case has a forward stage and renewal facts")
        if self.request_id is not None or self.claim_day is not None:
            raise ValueError("a RENEWAL case has no evaluation claim")
        if self.holdout_passed is not None:
            raise ValueError("a RENEWAL case opens no holdout")
        if self.selected_params is None or self.cohort != [self.selected_params]:
            raise ValueError("a RENEWAL case names exactly its deployed parameters")

    def _holdout_evidence_shape(self) -> None:
        """Ruling 15: the signed holdout evidence and the header say the same thing."""
        if self.kind == "RENEWAL":
            if self.evidence.get("holdout") is not None:
                raise ValueError("a RENEWAL case has no holdout evidence")
            return
        if "holdout" not in self.evidence:
            raise ValueError("an INITIAL case's evidence names its holdout (a result or null)")
        holdout = self.evidence["holdout"]
        if self.holdout_passed is None:
            if holdout is not None:
                raise ValueError(f"a {self.stage} case has null holdout evidence")
            return
        if not isinstance(holdout, dict) or type(holdout.get("passed")) is not bool:
            raise ValueError("evidence.holdout.passed must be true or false")
        if holdout["passed"] is not self.holdout_passed:
            raise ValueError("evidence.holdout.passed contradicts holdout_passed")


def initial_deploy_allowed(case: EvaluationCase) -> bool:
    """Rules first: complete, a passed holdout and every paper-v1 rule passed."""
    results = case.final_rule_results
    return (case.kind == "INITIAL" and case.stage == "COMPLETE" and case.holdout_passed is True
            and case.decision_state == STATE_PAPER_ELIGIBLE and case.ruleset_digest == PAPER_V1.digest
            and len(results) == len(PAPER_V1.rules) and {r.code for r in results} == PAPER_V1_CODES
            and all(r.passed for r in results))


def renewal_forward_complete(case: EvaluationCase) -> bool:
    facts = case.renewal
    return (case.kind == "RENEWAL" and case.stage == "FORWARD_COMPLETE" and facts is not None
            and facts.forward_sessions >= 1 and facts.incomplete_sessions == 0)


def offered_menu(case: EvaluationCase) -> tuple[str, ...]:
    qualifies = initial_deploy_allowed(case) if case.kind == "INITIAL" else renewal_forward_complete(case)
    return FULL_MENU if qualifies else NO_DEPLOY_MENU


def case_digest(body: Mapping[str, Any]) -> str:
    return "sha256:" + sha256_digest(CASE_DOMAIN, body)


def _signed_message(body: Mapping[str, Any]) -> bytes:
    return CASE_DOMAIN.encode("utf-8") + b"\n" + canonical_json_bytes(body)


def case_path(cases_dir: Path, digest: str) -> Path:
    if not isinstance(digest, str) or not _DIGEST.fullmatch(digest):
        raise CaseRefused("CASE_DIGEST_INVALID", "a case digest is sha256:<hex>")
    return Path(cases_dir) / f"sha256_{digest.split(':', 1)[1]}.json"


def default_cases_dir() -> Path:
    return Path("~/.local/share/mmr/artifacts/cases").expanduser()


def default_verify_dir() -> Path:
    return Path("~/.config/mmr/keys/verify").expanduser()


def write_evaluation_case(cases_dir: Path, case: EvaluationCase, signer: AttestationSigner) -> str:
    """Sign and store ``case`` once (immutable); an identical rewrite is a no-op."""
    body = case.model_dump()
    digest = case_digest(body)
    envelope = {"case": body, "case_digest": digest, "public_key_id": signer.public_key_id,
                "signature": signer.sign_message(_signed_message(body))}
    data = canonical_json_bytes(envelope)
    path = case_path(cases_dir, digest)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if path.read_bytes() != data:
            raise CaseRefused("CASE_EXISTS_DIFFERENT", f"{path.name} already holds other bytes")
        return digest
    staged = path.with_name(f".{path.name}.{uuid.uuid4().hex}")
    staged.write_bytes(data)
    os.replace(staged, path)
    return digest


def load_case_verify_keys(verify_dir: Path) -> dict[str, Ed25519PublicKey]:
    verify_dir = Path(verify_dir)
    paths = sorted(verify_dir.glob("*.pem")) if verify_dir.is_dir() else []
    keys: dict[str, Ed25519PublicKey] = {}
    for path in paths:
        try:
            key = load_verify_key(str(path))
        except (OSError, MalformedKey, InvalidKeyType) as exc:
            raise CaseRefused("CASE_VERIFY_KEYS_UNREADABLE", f"{path.name}: {type(exc).__name__}") from None
        keys[public_key_id(key)] = key
    if not keys:
        raise CaseRefused("CASE_VERIFY_KEYS_MISSING", f"no *.pem verify key under {verify_dir}")
    return keys


def _refuse_constant(name: str) -> Any:
    raise ValueError(f"{name} is not canonical JSON")


def load_verified_case(cases_dir: Path, digest: str, keys: Mapping[str, Ed25519PublicKey]) -> EvaluationCase:
    path = case_path(cases_dir, digest)
    if path.is_symlink() or not path.is_file():
        raise CaseRefused("CASE_NOT_FOUND", f"no case file {path.name}")
    data = path.read_bytes()
    if len(data) > MAX_CASE_BYTES:
        raise CaseRefused("CASE_MALFORMED", "the case file is too large")
    try:
        envelope = json.loads(data, parse_constant=_refuse_constant)
    except ValueError:
        raise CaseRefused("CASE_MALFORMED", "the case file is not JSON") from None
    if not isinstance(envelope, dict) or set(envelope) != _ENVELOPE_KEYS or not isinstance(envelope["case"], dict):
        raise CaseRefused("CASE_MALFORMED", f"a case file has exactly the keys {sorted(_ENVELOPE_KEYS)}")
    body = envelope["case"]
    try:
        recomputed = case_digest(body)
    except (TypeError, ValueError):
        raise CaseRefused("CASE_MALFORMED", "the case body is not canonical") from None
    if recomputed != digest or envelope["case_digest"] != digest:
        raise CaseRefused("CASE_DIGEST_MISMATCH", "the case body does not match its digest")
    key = keys.get(envelope["public_key_id"]) if isinstance(envelope["public_key_id"], str) else None
    if key is None:
        raise CaseRefused("CASE_KEY_UNKNOWN", "the case is signed by a key outside keys/verify")
    if not isinstance(envelope["signature"], str):
        raise CaseRefused("CASE_SIGNATURE_INVALID", "the signature is not text")
    try:
        verify_bytes(key, _signed_message(body), envelope["signature"])
    except BadSignature:
        raise CaseRefused("CASE_SIGNATURE_INVALID", "the case signature does not verify") from None
    try:
        return EvaluationCase.model_validate(body)
    except ValidationError as exc:
        raise CaseRefused("CASE_MALFORMED", f"{exc.error_count()} case field errors") from None
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/research/test_evaluation_request_and_case.py -q`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add trader/research/evaluation_request.py trader/research/evaluation_case.py \
  tests/automation/backtest_judge_fixtures.py tests/research/test_evaluation_request_and_case.py
git commit -m "$(cat <<'EOF'
feat: add the canonical evaluation request and the signed evaluation case

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 3: Journal tables and the evaluation claims

**Files:**
- Create: `trader/automation/backtest_judge_schema.py`, `trader/automation/evaluation_claims.py`
- Modify: `trader/data/schema_migrations.py` (docstring: "SP2c Plan 1 owns 110–114 and uses **110** (`evaluation_claims`) and **111** (`backtest_judgments`).")
- Modify: `tests/automation/backtest_judge_fixtures.py`
- Test: `tests/automation/test_evaluation_claims.py`

**Interfaces:**
- Consumes: `SchemaMigrator.apply(version, name, statements)`, `DuckDBConnection.transaction(fn)`; `BacktestJudgeConfig` (Task 1); `EvaluationRequestBody`, `canonical_request_json`, `evaluation_request_id` (Task 2).
- Produces:
  - `apply_backtest_judge_migrations(migrator) -> list[int]`; `cooling_until_in_tx(conn, strategy_key: str, today: date) -> Optional[date]`.
  - `utc(moment: datetime) -> datetime`, `ny_day(moment: datetime) -> date`.
  - `EvaluationClaim(request_id, body_json, strategy_key, ny_day, state, principal, claimed_at, updated_at)` with `.body() -> EvaluationRequestBody`, `.to_json() -> dict`.
  - `ClaimResult(status, claim)` with `.reply() -> dict`; `ClaimRefused(code, detail, *, retryable=False)` with `.reply() -> dict`.
  - `claim_row_in_tx(conn, request_id) -> Optional[EvaluationClaim]`.
  - `EvaluationClaims(db, *, config, now)`: `.claim(request_id, body, *, principal) -> ClaimResult`, `.get(request_id) -> Optional[EvaluationClaim]`, `.update(request_id, state) -> ClaimResult`.

- [ ] **Step 1: Extend the test helpers** (append to `tests/automation/backtest_judge_fixtures.py`)

```python
from dataclasses import replace

from trader.automation.backtest_judge_config import BacktestJudgeConfig
from trader.automation.backtest_judge_schema import apply_backtest_judge_migrations
from trader.data.duckdb_store import DuckDBConnection
from trader.data.schema_migrations import SchemaMigrator


def journal(tmp_path: Path) -> DuckDBConnection:
    db = DuckDBConnection.get_instance(str(tmp_path / "journal.duckdb"))
    apply_backtest_judge_migrations(SchemaMigrator(db))
    return db


def judge_config(**changes) -> BacktestJudgeConfig:
    return replace(BacktestJudgeConfig(strategy_allowlist=(KEY,)), **changes)


def insert_reject(db: DuckDBConnection, strategy_key: str, until: dt.date, judgment_id: str = "jdg-fixture-1") -> None:
    """A bare REJECT row, so the claim tests do not depend on the judgment store."""
    db.execute(
        "INSERT INTO backtest_judgments (judgment_id, case_digest, request_id, kind, verdict, strategy_key, "
        "body_json, body_digest, binding_json, cooldown_until_session, recorded_at, record_digest) "
        "VALUES (?, ?, ?, 'INITIAL', 'REJECT', ?, '{}', 'x', '{}', ?, ?, 'x')",
        [judgment_id, "sha256:" + judgment_id.encode().hex().ljust(64, "0")[:64], "sha256:fixture",
         strategy_key, until, NOW])


def count_claims(db: DuckDBConnection) -> int:
    return db.execute("SELECT COUNT(*) FROM evaluation_claims", fetch="one")[0]
```

- [ ] **Step 2: Write the failing tests** (`tests/automation/test_evaluation_claims.py`)

```python
"""SP2c Plan 1 Task 3: evaluation claims at the trader (spec 5.1 steps 3, 5, 6, 8; 6.1; 9 daily cap)."""
from __future__ import annotations

import datetime as dt
import threading

import pytest

from tests.automation.backtest_judge_fixtures import (
    KEY, NOW, ZERO, Clock, count_claims, insert_reject, journal, judge_config, request_body,
)
from trader.automation.evaluation_claims import ClaimRefused, EvaluationClaims
from trader.data.duckdb_store import DuckDBConnection
from trader.data.schema_migrations import SchemaMigrator
from trader.research.evaluation_request import evaluation_request_id


@pytest.fixture
def clock():
    return Clock(NOW)


@pytest.fixture
def db(tmp_path):
    return journal(tmp_path)


@pytest.fixture
def claims(db, clock):
    return EvaluationClaims(db, config=judge_config(), now=clock)


def claim(claims, body=None):
    body = body or request_body()
    return claims.claim(evaluation_request_id(body), body, principal="research")


def refusal(fn) -> str:
    with pytest.raises(ClaimRefused) as exc:
        fn()
    return exc.value.code


def test_migrations_110_and_111_create_the_claim_and_judgment_tables(db):
    tables = {r[0] for r in db.execute("SELECT table_name FROM information_schema.tables", fetch="all")}
    assert {"evaluation_claims", "backtest_judgments"} <= tables
    assert {110, 111} <= SchemaMigrator(db).applied_versions()


def test_a_new_claim_is_queued_on_its_new_york_day(claims):
    result = claim(claims)
    assert result.status == "ACCEPTED"
    assert (result.claim.state, result.claim.ny_day, result.claim.strategy_key) == ("QUEUED", dt.date(2026, 10, 8), KEY)
    assert result.claim.body() == request_body()


def test_a_retry_with_the_same_body_returns_the_claim_and_takes_no_slot(claims, db):    # review focus 2
    first = claim(claims)
    again = claim(claims)
    assert (again.status, again.claim) == ("EXISTING", first.claim) and count_claims(db) == 1


def test_the_same_id_with_another_body_is_a_conflict(claims, db):
    body = request_body()
    claims.claim(evaluation_request_id(body), body, principal="research")
    other = request_body(bar_size="1 min")
    assert refusal(lambda: claims.claim(evaluation_request_id(body), other, principal="research")) \
        == "EVALUATION_REQUEST_CONFLICT"
    assert count_claims(db) == 1


def test_a_caller_chosen_request_id_is_refused(claims, db):
    assert refusal(lambda: claims.claim(ZERO, request_body(), principal="research")) == "EVALUATION_REQUEST_ID_MISMATCH"
    assert count_claims(db) == 0


def test_the_trader_checks_the_allowlist_and_the_cohort_size_itself(db, clock):
    assert refusal(lambda: claim(EvaluationClaims(db, config=judge_config(max_cohort_points=1), now=clock))) \
        == "COHORT_TOO_LARGE"
    assert refusal(lambda: claim(EvaluationClaims(db, config=judge_config(strategy_allowlist=()), now=clock))) \
        == "STRATEGY_NOT_ALLOWED"
    assert count_claims(db) == 0


def test_the_day_cap_counts_every_claim_of_the_day_whatever_its_state(db, clock):
    claims = EvaluationClaims(db, config=judge_config(evaluations_per_day=2), now=clock)
    first = claim(claims, request_body(research_day="2026-10-01"))
    claims.update(first.claim.request_id, "FAILED")
    claim(claims, request_body(research_day="2026-10-02"))
    assert refusal(lambda: claim(claims, request_body(research_day="2026-10-03"))) == "EVALUATION_LIMIT_REACHED"


def test_the_cap_resets_at_new_york_midnight_not_at_utc_midnight(db, clock):
    claims = EvaluationClaims(db, config=judge_config(evaluations_per_day=1), now=clock)
    clock.now = dt.datetime(2026, 10, 9, 3, 59, tzinfo=dt.timezone.utc)          # 23:59 ET on Oct 8
    assert claim(claims, request_body(research_day="2026-10-01")).claim.ny_day == dt.date(2026, 10, 8)
    clock.now = dt.datetime(2026, 10, 9, 4, 1, tzinfo=dt.timezone.utc)           # 00:01 ET on Oct 9
    assert claim(claims, request_body(research_day="2026-10-02")).claim.ny_day == dt.date(2026, 10, 9)


def test_two_concurrent_claims_for_the_last_slot_accept_exactly_one(db, clock):     # review focus 1
    claims = EvaluationClaims(db, config=judge_config(evaluations_per_day=2), now=clock)
    claim(claims, request_body(research_day="2026-10-01"))
    barrier, outcomes = threading.Barrier(2), []

    def attempt(body):
        barrier.wait()
        try:
            outcomes.append(claim(claims, body).status)
        except ClaimRefused as refused:
            outcomes.append(refused.code)

    threads = [threading.Thread(target=attempt, args=(request_body(research_day=day),))
               for day in ("2026-10-02", "2026-10-03")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
    assert sorted(outcomes) == ["ACCEPTED", "EVALUATION_LIMIT_REACHED"] and count_claims(db) == 2


def test_a_rejected_strategy_key_cools_down_every_parameter_set(db, claims, clock):
    insert_reject(db, KEY, dt.date(2026, 10, 22))
    other_point = request_body(cohort=[{"RANGE_MINUTES": 45}])
    assert refusal(lambda: claim(claims, other_point)) == "FAMILY_COOLING_DOWN"
    clock.now = dt.datetime(2026, 10, 22, 20, 0, tzinfo=dt.timezone.utc)
    assert refusal(lambda: claim(claims, other_point)) == "FAMILY_COOLING_DOWN"
    clock.now = dt.datetime(2026, 10, 23, 14, 0, tzinfo=dt.timezone.utc)
    assert claim(claims, other_point).status == "ACCEPTED" and count_claims(db) == 1


def test_a_cooldown_of_another_strategy_key_does_not_block(db, claims):
    insert_reject(db, "strategies/opening_range_breakout.py:Other", dt.date(2026, 10, 22))
    assert claim(claims).status == "ACCEPTED"


def test_states_only_move_forward_and_a_repeat_is_a_no_op(claims):
    request_id = claim(claims).claim.request_id
    assert claims.update(request_id, "RUNNING").status == "UPDATED"
    assert claims.update(request_id, "RUNNING").status == "UNCHANGED"
    assert claims.update(request_id, "DONE").claim.state == "DONE"
    for backward in ("RUNNING", "FAILED"):
        assert refusal(lambda: claims.update(request_id, backward)) == "CLAIM_STATE_BACKWARD"
    assert refusal(lambda: claims.update(ZERO, "RUNNING")) == "CLAIM_UNKNOWN"


def test_a_restarted_trader_reads_queued_and_running_claims_back(tmp_path, claims, clock):
    queued = claim(claims, request_body(research_day="2026-10-01")).claim
    running = claim(claims, request_body(research_day="2026-10-02")).claim
    claims.update(running.request_id, "RUNNING")
    fresh = EvaluationClaims(DuckDBConnection(str(tmp_path / "journal.duckdb")), config=judge_config(), now=clock)
    assert fresh.get(queued.request_id).state == "QUEUED"
    assert fresh.get(running.request_id).state == "RUNNING"
    assert fresh.get(ZERO) is None
```

- [ ] **Step 3: Run the tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/automation/test_evaluation_claims.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'trader.automation.backtest_judge_schema'`.

- [ ] **Step 4: Write the implementation**

`trader/automation/backtest_judge_schema.py`:

```python
"""Journal tables of the SP2c backtest judge (migrations 110 and 111; 112-114 stay free)."""
from __future__ import annotations

import datetime as dt
from typing import Any, Optional

CLAIMS_MIGRATION_VERSION = 110
JUDGMENTS_MIGRATION_VERSION = 111

_CLAIMS = """CREATE TABLE IF NOT EXISTS evaluation_claims (
    request_id VARCHAR PRIMARY KEY, body_json VARCHAR NOT NULL, strategy_key VARCHAR NOT NULL,
    ny_day DATE NOT NULL,
    state VARCHAR NOT NULL CHECK (state IN ('QUEUED','RUNNING','DONE','FAILED')),
    principal VARCHAR NOT NULL, claimed_at TIMESTAMPTZ NOT NULL, updated_at TIMESTAMPTZ NOT NULL)"""

_JUDGMENTS = """CREATE TABLE IF NOT EXISTS backtest_judgments (
    judgment_id VARCHAR PRIMARY KEY, case_digest VARCHAR NOT NULL UNIQUE, request_id VARCHAR,
    kind VARCHAR NOT NULL CHECK (kind IN ('INITIAL','RENEWAL')),
    verdict VARCHAR NOT NULL CHECK (verdict IN ('DEPLOY','SHADOW','REJECT','NO_VERDICT')),
    strategy_key VARCHAR NOT NULL, body_json VARCHAR NOT NULL, body_digest VARCHAR NOT NULL,
    binding_json VARCHAR NOT NULL, cooldown_until_session DATE, recorded_at TIMESTAMPTZ NOT NULL,
    record_digest VARCHAR NOT NULL,
    CHECK ((kind = 'INITIAL') = (request_id IS NOT NULL)),
    CHECK ((verdict = 'REJECT') = (cooldown_until_session IS NOT NULL)))"""


def apply_backtest_judge_migrations(migrator: Any) -> list[int]:
    applied = []
    if migrator.apply(CLAIMS_MIGRATION_VERSION, "sp2c_evaluation_claims", (_CLAIMS,)):
        applied.append(CLAIMS_MIGRATION_VERSION)
    if migrator.apply(JUDGMENTS_MIGRATION_VERSION, "sp2c_backtest_judgments", (_JUDGMENTS,)):
        applied.append(JUDGMENTS_MIGRATION_VERSION)
    return applied


def cooling_until_in_tx(conn: Any, strategy_key: str, today: dt.date) -> Optional[dt.date]:
    """The last cooldown session of ``strategy_key`` if it still cools down on ``today``, else None."""
    row = conn.execute(
        "SELECT MAX(cooldown_until_session) FROM backtest_judgments WHERE strategy_key = ? AND verdict = 'REJECT'",
        [strategy_key]).fetchone()
    until = None if row is None else row[0]
    return until if until is not None and today <= until else None
```

`trader/automation/evaluation_claims.py`:

```python
"""Evaluation claims: the trader's daily slots for research evaluations (SP2c spec 5.1, 5.2 item 1).

One durable row per request id. A claim is written in one transaction that
checks, in order: the same id (retry or conflict), the id is the body's digest,
the allowlist and cohort size, the strategy key's cooldown, and the New York
day's cap. The journal file lock serialises claims, so the last slot goes to
exactly one caller. States only move forward.
"""
from __future__ import annotations

import datetime as dt
import json
from dataclasses import dataclass, replace
from typing import Any, Callable, Optional

from trader.automation.backtest_judge_config import BacktestJudgeConfig
from trader.automation.backtest_judge_schema import cooling_until_in_tx
from trader.automation.calendar_policy import ET
from trader.research.evaluation_request import EvaluationRequestBody, canonical_request_json, evaluation_request_id

EVALUATION_REQUEST_CONFLICT = "EVALUATION_REQUEST_CONFLICT"
EVALUATION_REQUEST_ID_MISMATCH = "EVALUATION_REQUEST_ID_MISMATCH"
STRATEGY_NOT_ALLOWED = "STRATEGY_NOT_ALLOWED"
COHORT_TOO_LARGE = "COHORT_TOO_LARGE"
FAMILY_COOLING_DOWN = "FAMILY_COOLING_DOWN"
EVALUATION_LIMIT_REACHED = "EVALUATION_LIMIT_REACHED"
CLAIM_UNKNOWN = "CLAIM_UNKNOWN"
CLAIM_STATE_BACKWARD = "CLAIM_STATE_BACKWARD"
_RANK = {"QUEUED": 0, "RUNNING": 1, "DONE": 2, "FAILED": 2}
_COLUMNS = "request_id, body_json, strategy_key, ny_day, state, principal, claimed_at, updated_at"


def utc(moment: dt.datetime) -> dt.datetime:
    if moment.tzinfo is None or moment.utcoffset() is None:
        raise ValueError("trader times must be timezone-aware")
    return moment.astimezone(dt.timezone.utc)


def ny_day(moment: dt.datetime) -> dt.date:
    return utc(moment).astimezone(ET).date()


class ClaimRefused(Exception):
    def __init__(self, code: str, detail: str, *, retryable: bool = False):
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail
        self.retryable = retryable

    def reply(self) -> dict:
        return {"status": "REFUSED", "code": self.code, "detail": self.detail, "retryable": self.retryable,
                "claim": None}


@dataclass(frozen=True)
class EvaluationClaim:
    request_id: str
    body_json: str
    strategy_key: str
    ny_day: dt.date
    state: str
    principal: str
    claimed_at: dt.datetime
    updated_at: dt.datetime

    def body(self) -> EvaluationRequestBody:
        return EvaluationRequestBody.model_validate(json.loads(self.body_json))

    def to_json(self) -> dict:
        return {"request_id": self.request_id, "strategy_key": self.strategy_key, "ny_day": self.ny_day.isoformat(),
                "state": self.state, "claimed_at": self.claimed_at.isoformat(),
                "updated_at": self.updated_at.isoformat(), "body": json.loads(self.body_json)}


@dataclass(frozen=True)
class ClaimResult:
    status: str   # ACCEPTED | EXISTING | UPDATED | UNCHANGED
    claim: EvaluationClaim

    def reply(self) -> dict:
        return {"status": self.status, "code": None, "detail": None, "retryable": False,
                "claim": self.claim.to_json()}


def claim_row_in_tx(conn: Any, request_id: str) -> Optional[EvaluationClaim]:
    row = conn.execute(f"SELECT {_COLUMNS} FROM evaluation_claims WHERE request_id = ?", [request_id]).fetchone()
    if row is None:
        return None
    return EvaluationClaim(row[0], row[1], row[2], row[3], row[4], row[5], utc(row[6]), utc(row[7]))


class EvaluationClaims:
    def __init__(self, db: Any, *, config: BacktestJudgeConfig, now: Callable[[], dt.datetime]):
        self._db = db
        self._config = config
        self._now = now

    def claim(self, request_id: str, body: EvaluationRequestBody, *, principal: str) -> ClaimResult:
        canonical = canonical_request_json(body)
        expected_id = evaluation_request_id(body)
        now = utc(self._now())
        day = ny_day(now)

        def write(conn: Any) -> ClaimResult:
            existing = claim_row_in_tx(conn, request_id)
            if existing is not None:
                if existing.body_json != canonical:
                    raise ClaimRefused(EVALUATION_REQUEST_CONFLICT, "this request id already names another body")
                return ClaimResult("EXISTING", existing)
            if request_id != expected_id:
                raise ClaimRefused(EVALUATION_REQUEST_ID_MISMATCH, "the request id must be the digest of the body")
            self._check_config(body)
            until = cooling_until_in_tx(conn, body.strategy_key, day)
            if until is not None:
                raise ClaimRefused(FAMILY_COOLING_DOWN,
                                   f"{body.strategy_key} cools down until session {until.isoformat()}")
            used = conn.execute("SELECT COUNT(*) FROM evaluation_claims WHERE ny_day = ?", [day]).fetchone()[0]
            if used >= self._config.evaluations_per_day:
                raise ClaimRefused(EVALUATION_LIMIT_REACHED, f"{used} of {self._config.evaluations_per_day} "
                                                             f"evaluations already claimed on {day.isoformat()}")
            conn.execute(f"INSERT INTO evaluation_claims ({_COLUMNS}) VALUES (?, ?, ?, ?, 'QUEUED', ?, ?, ?)",
                         [request_id, canonical, body.strategy_key, day, principal, now, now])
            return ClaimResult("ACCEPTED", EvaluationClaim(request_id, canonical, body.strategy_key, day, "QUEUED",
                                                           principal, now, now))
        return self._db.transaction(write)

    def _check_config(self, body: EvaluationRequestBody) -> None:
        if not self._config.allows(body.strategy_key):
            raise ClaimRefused(STRATEGY_NOT_ALLOWED, f"{body.strategy_key} is not on ai_paper.backtest_judge.strategy_allowlist")
        if len(body.cohort) > self._config.max_cohort_points:
            raise ClaimRefused(COHORT_TOO_LARGE, f"{len(body.cohort)} points; the limit is {self._config.max_cohort_points}")

    def get(self, request_id: str) -> Optional[EvaluationClaim]:
        return self._db.transaction(lambda conn: claim_row_in_tx(conn, request_id))

    def update(self, request_id: str, state: str) -> ClaimResult:
        if state not in ("RUNNING", "DONE", "FAILED"):
            raise ValueError(f"unknown claim state {state!r}")
        now = utc(self._now())

        def write(conn: Any) -> ClaimResult:
            claim = claim_row_in_tx(conn, request_id)
            if claim is None:
                raise ClaimRefused(CLAIM_UNKNOWN, "the trader holds no claim with this request id")
            if claim.state == state:
                return ClaimResult("UNCHANGED", claim)
            if _RANK[state] <= _RANK[claim.state]:
                raise ClaimRefused(CLAIM_STATE_BACKWARD, f"{claim.state} cannot move to {state}")
            conn.execute("UPDATE evaluation_claims SET state = ?, updated_at = ? WHERE request_id = ?",
                         [state, now, request_id])
            return ClaimResult("UPDATED", replace(claim, state=state, updated_at=now))
        return self._db.transaction(write)
```

Also update the `trader/data/schema_migrations.py` module docstring: after the "SP2 Plan 3 owns 100–104" line add "SP2c Plan 1 owns 110–114 and uses **110** (``evaluation_claims``) and **111** (``backtest_judgments``)."

- [ ] **Step 5: Run the tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/automation/test_evaluation_claims.py -q`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add trader/automation/backtest_judge_schema.py trader/automation/evaluation_claims.py \
  trader/data/schema_migrations.py tests/automation/backtest_judge_fixtures.py tests/automation/test_evaluation_claims.py
git commit -m "$(cat <<'EOF'
feat: add trader evaluation claims with a daily cap and cooldown check

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 4: Backtest judgments and the cooldown start

**Files:**
- Create: `trader/automation/backtest_judge_wire.py` (judgment body), `trader/automation/backtest_judgments.py`
- Modify: `trader/automation/calendar_policy.py`: a default exchange_calendars calendar ends about one year after it is built, so a 120-session cooldown (or a trader up for months) ran past it and `nth_session_after` leaked `DateOutOfBounds`. `XNYSCalendarPolicy.sessions_in_range` rebuilds a policy-owned calendar with `end = Dec 31 of (end.year + 5)` when asked past its last session (xcals caches by arguments); an injected calendar is kept. `nth_session_after` turns a remaining `DateOutOfBounds` into `JudgmentRefused("COOLDOWN_CALENDAR_UNAVAILABLE")`. Tests: `test_nth_session_after_serves_a_day_past_the_end_of_the_calendar_it_was_built_with`, `test_a_long_cooldown_accepted_by_config_still_records_a_reject` (N=120, clock 30 days before the default calendar's end, expected session read from a separate wide calendar), `test_a_calendar_that_cannot_reach_the_cooldown_refuses_the_reject_and_writes_nothing`.
- Modify: `tests/automation/backtest_judge_fixtures.py`
- Test: `tests/automation/test_backtest_judgments.py`

**Interfaces:**
- Consumes: `claim_row_in_tx`, `utc`, `ny_day`, `EvaluationClaims` (Task 3); `load_verified_case`, `load_case_verify_keys`, `offered_menu`, `initial_deploy_allowed`, `renewal_forward_complete`, `CaseRefused`, `EvaluationCase`, `FULL_MENU`, `NO_DEPLOY_MENU` (Task 2); `split_strategy_key` (Task 1); `XNYSCalendarPolicy.sessions_in_range(start, end) -> list[date]`.
- Produces:
  - `RecordBacktestJudgmentRequest` (fields in Cross-plan additions) with `.decided_at_utc() -> datetime`, `.digest_body() -> dict`; `DeployNarrative`; `NARRATIVE_FIELDS`; `parse_aware(text) -> datetime`.
  - `nth_session_after(calendar, day: date, sessions: int) -> date`.
  - `RenewalStatus`, `RenewalChecks` (Protocol), `NoRenewalVersions`.
  - `BacktestJudgment` (`judgment_id, case_digest, request_id, kind, verdict, strategy_key, body, binding, cooldown_until_session, recorded_at`, `.to_json()`), `JudgmentRefused(code, detail, *, retryable=False)`.
  - `BacktestJudgments(db, *, config, calendar, cases_dir, verify_dir, now, renewals=None)`: `.record(request) -> dict` (reply body), `.get(judgment_id) -> Optional[BacktestJudgment]`, `.get_by_case(case_digest) -> Optional[BacktestJudgment]`.

- [ ] **Step 1: Extend the test helpers** (append to `tests/automation/backtest_judge_fixtures.py`)

```python
from trader.automation.backtest_judge_wire import NARRATIVE_FIELDS, RecordBacktestJudgmentRequest
from trader.automation.backtest_judgments import BacktestJudgments
from trader.automation.calendar_policy import XNYSCalendarPolicy
from trader.automation.evaluation_claims import EvaluationClaims
from trader.research.canonical import canonical_json_bytes
from trader.research.evaluation_case import FULL_MENU, case_digest, case_path, write_evaluation_case

NARRATIVE = {name: f"{name} written by Jev" for name in NARRATIVE_FIELDS}


@dataclass
class World:
    db: DuckDBConnection
    clock: Clock
    claims: EvaluationClaims
    judgments: BacktestJudgments
    keys: CaseKeys


def world(tmp_path: Path, *, renewals=None, **config) -> World:
    db, clock, keys, cfg = journal(tmp_path), Clock(NOW), case_keys(tmp_path), judge_config(**config)
    judgments = BacktestJudgments(db, config=cfg, calendar=XNYSCalendarPolicy(), cases_dir=keys.cases_dir,
                                  verify_dir=keys.verify_dir, now=clock, renewals=renewals)
    return World(db, clock, EvaluationClaims(db, config=cfg, now=clock), judgments, keys)


def finished(w: World, body: EvaluationRequestBody | None = None, *, state: str = "DONE", signer=None,
             **case_changes) -> str:
    """Claim, finish and sign one evaluation; return its case digest."""
    body = body or request_body()
    w.claims.claim(evaluation_request_id(body), body, principal="research")
    w.claims.update(evaluation_request_id(body), state)
    return write_evaluation_case(w.keys.cases_dir, make_case(body, **case_changes), signer or w.keys.signer)


def judgment(case_digest: str, verdict: str = "REJECT", menu=FULL_MENU, **changes) -> RecordBacktestJudgmentRequest:
    raw = {"judgment_id": "jdg-00000001", "case_digest": case_digest, "kind": "INITIAL", "renewal_of_version": None,
           "verdict": verdict, "menu": list(menu), "jev_model": "openrouter/jev-1", "jev_attempt_ref": "att-1",
           "decided_at": NOW.isoformat(), "narrative": dict(NARRATIVE) if verdict == "DEPLOY" else None}
    raw.update(changes)
    return RecordBacktestJudgmentRequest.model_validate(raw)


def count_judgments(db: DuckDBConnection) -> int:
    return db.execute("SELECT COUNT(*) FROM backtest_judgments", fetch="one")[0]


def write_unchecked_case(cases_dir: Path, body: dict, signer: AttestationSigner) -> str:
    """Sign a raw case body without the model (a buggy or hostile signer); return its digest."""
    digest = case_digest(body)
    message = CASE_DOMAIN.encode("utf-8") + b"\n" + canonical_json_bytes(body)
    envelope = {"case": body, "case_digest": digest, "public_key_id": signer.public_key_id,
                "signature": signer.sign_message(message)}
    path = case_path(cases_dir, digest)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(canonical_json_bytes(envelope))
    return digest
```

- [ ] **Step 2: Write the failing tests** (`tests/automation/test_backtest_judgments.py`)

```python
"""SP2c Plan 1 Task 4: durable judgments, rules first, one per case, cooldown on REJECT (spec 5.2 items 2-3)."""
from __future__ import annotations

import datetime as dt
import json

import pytest
from pydantic import ValidationError

from tests.automation.backtest_judge_fixtures import (
    FILE_HASH, NARRATIVE, VERSION, World, case_body, count_judgments, failed_results, finished, judge_config,
    judgment, make_case, renewal_case_body, request_body, world, write_unchecked_case,
)
from trader.automation.backtest_judgments import (
    BacktestJudgments, JudgmentRefused, RenewalStatus, nth_session_after,
)
from trader.automation.calendar_policy import XNYSCalendarPolicy
from trader.automation.evaluation_claims import ClaimRefused
from trader.data.duckdb_store import DuckDBConnection
from trader.research.evaluation_case import NO_DEPLOY_MENU, EvaluationCase, case_path, write_evaluation_case
from trader.research.evaluation_request import evaluation_request_id
from trader.research.signing import AttestationSigner


def claim_status(w: World, body) -> str:
    try:
        return w.claims.claim(evaluation_request_id(body), body, principal="research").status
    except ClaimRefused as refused:
        return refused.code


def test_a_deploy_on_a_complete_passing_case_is_recorded_with_its_binding(tmp_path):
    w = world(tmp_path)
    reply = w.judgments.record(judgment(finished(w), "DEPLOY"))
    assert (reply["status"], reply["verdict"], reply["cooldown_until_session"]) == ("RECORDED", "DEPLOY", None)
    stored = w.judgments.get("jdg-00000001")
    assert stored.binding["strategy_file_hash"] == FILE_HASH and stored.binding["params"] == {"RANGE_MINUTES": 15}
    assert stored.binding["class_name"] == "OpeningRangeBreakout" and stored.body["narrative"] == NARRATIVE
    assert stored.request_id == evaluation_request_id(request_body())


def test_rules_first_a_rule_failing_case_is_never_deployed(tmp_path):                  # review focus 4
    w = world(tmp_path)
    digest = finished(w, stage="HOLDOUT_FAILED", decision_state="CANDIDATE", final_rule_results=failed_results())
    for request in (judgment(digest, "DEPLOY"), judgment(digest, "SHADOW")):          # both claim the full menu
        assert w.judgments.record(request)["code"] == "JUDGMENT_MENU_MISMATCH"
    assert count_judgments(w.db) == 0
    assert w.judgments.record(judgment(digest, "SHADOW", menu=NO_DEPLOY_MENU))["status"] == "RECORDED"


def test_a_signed_complete_case_whose_holdout_failed_is_never_deployed(tmp_path):  # review focus 4, ruling 15
    w = world(tmp_path)
    body = request_body()
    w.claims.claim(evaluation_request_id(body), body, principal="research")
    w.claims.update(evaluation_request_id(body), "DONE")
    lying = case_body(body)                  # header: COMPLETE, holdout_passed True, PAPER_ELIGIBLE, every rule passed
    lying["evidence"]["holdout"]["passed"] = False      # ...but the signed holdout evidence failed (PR #91 round 2)
    with pytest.raises(ValidationError):
        EvaluationCase.model_validate(lying)
    digest = write_unchecked_case(w.keys.cases_dir, lying, w.keys.signer)
    for verdict in ("DEPLOY", "SHADOW"):
        reply = w.judgments.record(judgment(digest, verdict))
        assert (reply["status"], reply["code"], reply["cooldown_until_session"]) == ("REFUSED", "CASE_MALFORMED", None)
    assert count_judgments(w.db) == 0 and w.judgments.get_by_case(digest) is None    # no DEPLOY row, ever
    assert claim_status(w, request_body(cohort=[{"RANGE_MINUTES": 45}])) == "ACCEPTED"     # no cooldown started


def test_a_pre_holdout_failure_reaches_jev_for_shadow_or_reject(tmp_path):
    w = world(tmp_path)
    digest = finished(w, stage="PRE_HOLDOUT_FAILED")
    assert w.judgments.record(judgment(digest, "REJECT", menu=NO_DEPLOY_MENU))["status"] == "RECORDED"
    assert w.judgments.get("jdg-00000001").binding["artifact_id"] is None


@pytest.mark.parametrize("damage,code", [
    ("body", "CASE_DIGEST_MISMATCH"), ("key", "CASE_KEY_UNKNOWN"), ("gone", "CASE_NOT_FOUND"),
])
def test_a_tampered_foreign_or_missing_case_records_nothing(tmp_path, damage, code):     # review focus 4
    w = world(tmp_path)
    digest = finished(w, signer=AttestationSigner.generate() if damage == "key" else None)
    path = case_path(w.keys.cases_dir, digest)
    if damage == "body":
        envelope = json.loads(path.read_text())
        envelope["case"]["bar_size"] = "1 min"
        path.write_text(json.dumps(envelope))
    if damage == "gone":
        path.unlink()
    reply = w.judgments.record(judgment(digest, "REJECT"))
    assert (reply["status"], reply["code"]) == ("REFUSED", code)
    assert count_judgments(w.db) == 0
    assert claim_status(w, request_body(cohort=[{"RANGE_MINUTES": 45}])) == "ACCEPTED"     # no cooldown started


def test_one_judgment_per_case_and_per_evaluation(tmp_path):                           # review focus 3
    w = world(tmp_path)
    digest = finished(w)
    assert w.judgments.record(judgment(digest, "SHADOW"))["status"] == "RECORDED"
    assert w.judgments.record(judgment(digest, "SHADOW"))["status"] == "EXISTING"
    assert w.judgments.record(judgment(digest, "REJECT"))["code"] == "JUDGMENT_CONFLICT"      # same id, other body
    assert w.judgments.record(judgment(digest, "SHADOW", judgment_id="jdg-00000002"))["code"] == "JUDGMENT_CONFLICT"
    second_case = write_evaluation_case(
        w.keys.cases_dir, make_case(request_body(), created_at="2026-10-08T21:31:00+00:00"), w.keys.signer)
    assert w.judgments.record(judgment(second_case, "SHADOW", judgment_id="jdg-00000003"))["code"] \
        == "JUDGMENT_CONFLICT"                                                             # same evaluation
    assert count_judgments(w.db) == 1


def test_a_retry_that_only_respells_the_decided_time_is_the_same_judgment(tmp_path):     # review focus 3
    w = world(tmp_path)
    digest = finished(w)
    w.judgments.record(judgment(digest, "SHADOW", decided_at="2026-10-08T21:30:00+00:00"))
    for spelling in ("2026-10-08T21:30:00Z", "2026-10-08T17:30:00-04:00"):
        assert w.judgments.record(judgment(digest, "SHADOW", decided_at=spelling))["status"] == "EXISTING"


def test_reject_cools_down_the_strategy_key_for_ten_sessions(tmp_path):
    w = world(tmp_path)
    assert w.judgments.record(judgment(finished(w), "REJECT"))["cooldown_until_session"] == "2026-10-22"
    another = request_body(cohort=[{"RANGE_MINUTES": 45}], research_day="2026-10-09")
    w.clock.now = dt.datetime(2026, 10, 22, 14, 0, tzinfo=dt.timezone.utc)
    assert claim_status(w, another) == "FAMILY_COOLING_DOWN"
    w.clock.now = dt.datetime(2026, 10, 23, 14, 0, tzinfo=dt.timezone.utc)
    assert claim_status(w, another) == "ACCEPTED"


def test_shadow_and_no_verdict_start_no_cooldown(tmp_path):
    w = world(tmp_path)
    shadow_case = finished(w, request_body(research_day="2026-10-01"))
    silent_case = finished(w, request_body(research_day="2026-10-02"))
    assert w.judgments.record(judgment(shadow_case, "SHADOW"))["cooldown_until_session"] is None
    assert w.judgments.record(judgment(silent_case, "NO_VERDICT", judgment_id="jdg-00000002"))["status"] == "RECORDED"
    assert claim_status(w, request_body(research_day="2026-10-03")) == "ACCEPTED"


def test_a_no_verdict_without_a_model_call_is_recorded_with_a_null_attempt(tmp_path):
    w = world(tmp_path)
    case = finished(w)
    reply = w.judgments.record(judgment(case, "NO_VERDICT", jev_attempt_ref=None))
    assert (reply["status"], reply["cooldown_until_session"]) == ("RECORDED", None)
    assert w.judgments.get("jdg-00000001").body["jev_attempt_ref"] is None
    assert w.judgments.record(judgment(case, "NO_VERDICT", jev_attempt_ref=None))["status"] == "EXISTING"


def test_a_case_needs_its_own_finished_claim(tmp_path):
    w = world(tmp_path)
    body = request_body()
    w.claims.claim(evaluation_request_id(body), body, principal="research")              # still QUEUED
    queued = write_evaluation_case(w.keys.cases_dir, make_case(body), w.keys.signer)
    reply = w.judgments.record(judgment(queued, "SHADOW"))
    assert (reply["code"], reply["retryable"]) == ("CASE_CLAIM_NOT_FINISHED", True)
    orphan = write_evaluation_case(w.keys.cases_dir, make_case(request_body(research_day="2026-10-05")), w.keys.signer)
    assert w.judgments.record(judgment(orphan, "SHADOW", judgment_id="jdg-00000002"))["code"] == "CASE_CLAIM_UNKNOWN"
    w.claims.update(evaluation_request_id(body), "DONE")
    other_day = write_evaluation_case(w.keys.cases_dir, make_case(body, claim_day="2026-10-07"), w.keys.signer)
    assert w.judgments.record(judgment(other_day, "SHADOW", judgment_id="jdg-00000003"))["code"] \
        == "CASE_CLAIM_MISMATCH"


def test_a_decision_time_ahead_of_the_trader_is_refused(tmp_path):
    w = world(tmp_path)
    reply = w.judgments.record(judgment(finished(w), "SHADOW", decided_at="2026-10-08T21:40:00+00:00"))
    assert reply["code"] == "DECIDED_IN_FUTURE"


def test_a_renewal_waits_for_plan_5(tmp_path):
    w = world(tmp_path)
    digest = write_evaluation_case(w.keys.cases_dir, EvaluationCase.model_validate(renewal_case_body()), w.keys.signer)
    reply = w.judgments.record(judgment(digest, "SHADOW", kind="RENEWAL", renewal_of_version=VERSION))
    assert reply["code"] == "RENEWAL_NOT_SUPPORTED" and count_judgments(w.db) == 0


def test_a_renewal_port_that_blocks_deploy_still_lets_jev_shadow(tmp_path):
    class ExpiredBundle:
        def status(self, case, *, now):
            return RenewalStatus(deploy_block_code="BUNDLE_EXPIRED", detail="attestation expired")

    w = world(tmp_path, renewals=ExpiredBundle())
    digest = write_evaluation_case(w.keys.cases_dir, EvaluationCase.model_validate(renewal_case_body()), w.keys.signer)
    deploy = judgment(digest, "DEPLOY", kind="RENEWAL", renewal_of_version=VERSION)
    assert w.judgments.record(deploy)["code"] == "BUNDLE_EXPIRED"
    shadow = judgment(digest, "SHADOW", kind="RENEWAL", renewal_of_version=VERSION, judgment_id="jdg-00000002")
    assert w.judgments.record(shadow)["status"] == "RECORDED"


def test_an_edited_judgment_reads_as_tampered(tmp_path):
    w = world(tmp_path)
    w.judgments.record(judgment(finished(w), "SHADOW"))
    w.db.execute("UPDATE backtest_judgments SET verdict = 'DEPLOY' WHERE judgment_id = 'jdg-00000001'")
    with pytest.raises(JudgmentRefused) as exc:
        w.judgments.get("jdg-00000001")
    assert exc.value.code == "JUDGMENT_TAMPERED"


def test_a_judgment_is_found_by_its_case(tmp_path):
    w = world(tmp_path)
    case = finished(w)
    w.judgments.record(judgment(case, "SHADOW"))
    assert w.judgments.get_by_case(case) == w.judgments.get("jdg-00000001")
    assert w.judgments.get_by_case("sha256:" + "e" * 64) is None


def test_judgments_survive_a_restart(tmp_path):
    w = world(tmp_path)
    w.judgments.record(judgment(finished(w), "REJECT"))
    fresh = BacktestJudgments(DuckDBConnection(str(tmp_path / "journal.duckdb")), config=judge_config(),
                              calendar=XNYSCalendarPolicy(), cases_dir=w.keys.cases_dir,
                              verify_dir=w.keys.verify_dir, now=w.clock)
    assert fresh.get("jdg-00000001") == w.judgments.get("jdg-00000001")
    assert fresh.get("jdg-00000009") is None


def test_nth_session_after_skips_weekends_and_holidays():
    calendar = XNYSCalendarPolicy()
    assert nth_session_after(calendar, dt.date(2026, 10, 8), 10) == dt.date(2026, 10, 22)
    assert nth_session_after(calendar, dt.date(2026, 11, 25), 1) == dt.date(2026, 11, 27)    # Thanksgiving
    assert nth_session_after(calendar, dt.date(2026, 10, 10), 1) == dt.date(2026, 10, 12)    # Saturday


@pytest.mark.parametrize("changes", [
    {"verdict": "DEPLOY", "narrative": None},
    {"verdict": "DEPLOY", "narrative": {**NARRATIVE, "episode_dominance": "   "}},
    {"verdict": "SHADOW", "narrative": NARRATIVE},
    {"verdict": "DEPLOY", "menu": NO_DEPLOY_MENU},
    {"menu": ("REJECT", "SHADOW")},
    {"decided_at": "2026-10-08T21:30:00"},
    {"renewal_of_version": VERSION},
    {"kind": "RENEWAL"},
    {"judgment_id": "short"},
    {"jev_model": "has spaces"},
    {"jev_attempt_ref": None},
    {"verdict": "DEPLOY", "narrative": NARRATIVE, "jev_attempt_ref": None},
    {"jev_attempt_ref": "att none"},
])
def test_a_malformed_judgment_body_is_refused_by_the_wire_model(changes):
    with pytest.raises(ValidationError):
        judgment("sha256:" + "c" * 64, **changes)
```

- [ ] **Step 3: Run the tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/automation/test_backtest_judgments.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'trader.automation.backtest_judge_wire'`.

- [ ] **Step 4: Write the wire model** (`trader/automation/backtest_judge_wire.py`)

```python
"""Typed RPC bodies of the SP2c backtest-judge methods (spec 5.1 table; 5.2 items 1-3, 7)."""
from __future__ import annotations

import datetime as dt
import re
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, model_validator

from trader.research.evaluation_case import FULL_MENU, NO_DEPLOY_MENU

DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
JUDGMENT_ID = re.compile(r"^[A-Za-z0-9_-]{8,96}$")
JEV_MODEL = re.compile(r"^[A-Za-z0-9_./:@+-]{1,128}$")
ATTEMPT_REF = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")
# The eight narrative fields of trader.research.review.OperatorReview (design §8.5).
NARRATIVE_FIELDS = ("economic_rationale", "edge_survives_costs", "known_failure_regimes",
                    "data_and_survivorship_limits", "parameter_sensitivity", "operational_dependencies",
                    "capacity_and_decay", "episode_dominance")
MAX_NARRATIVE_CHARS = 4000


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


def _matching(pattern: re.Pattern, name: str, value: str) -> str:
    if not pattern.fullmatch(value):
        raise ValueError(f"{name} must match {pattern.pattern}")
    return value


def parse_aware(text: str) -> dt.datetime:
    try:
        moment = dt.datetime.fromisoformat(text)
    except ValueError:
        raise ValueError(f"{text!r} is not an ISO-8601 time") from None
    if moment.tzinfo is None or moment.utcoffset() is None:
        raise ValueError("times must carry an offset")
    return moment.astimezone(dt.timezone.utc)


class DeployNarrative(_Strict):
    economic_rationale: str
    edge_survives_costs: str
    known_failure_regimes: str
    data_and_survivorship_limits: str
    parameter_sensitivity: str
    operational_dependencies: str
    capacity_and_decay: str
    episode_dominance: str

    @model_validator(mode="after")
    def _filled(self) -> "DeployNarrative":
        for name in NARRATIVE_FIELDS:
            text = getattr(self, name)
            if not text.strip() or len(text) > MAX_NARRATIVE_CHARS:
                raise ValueError(f"{name} must be 1-{MAX_NARRATIVE_CHARS} characters and not blank")
        return self


class RecordBacktestJudgmentRequest(_Strict):
    judgment_id: str
    case_digest: str
    kind: Literal["INITIAL", "RENEWAL"]
    renewal_of_version: Optional[str]
    verdict: Literal["DEPLOY", "SHADOW", "REJECT", "NO_VERDICT"]
    menu: list[Literal["DEPLOY", "SHADOW", "REJECT"]]
    jev_model: str
    jev_attempt_ref: Optional[str]
    decided_at: str
    narrative: Optional[DeployNarrative]

    @model_validator(mode="after")
    def _shape(self) -> "RecordBacktestJudgmentRequest":
        _matching(JUDGMENT_ID, "judgment_id", self.judgment_id)
        _matching(DIGEST, "case_digest", self.case_digest)
        _matching(JEV_MODEL, "jev_model", self.jev_model)
        if self.jev_attempt_ref is not None:
            _matching(ATTEMPT_REF, "jev_attempt_ref", self.jev_attempt_ref)
        elif self.verdict != "NO_VERDICT":
            raise ValueError("only a NO_VERDICT may lack a model attempt (no call was sent)")
        parse_aware(self.decided_at)
        if tuple(self.menu) not in (FULL_MENU, NO_DEPLOY_MENU):
            raise ValueError(f"menu must be {list(FULL_MENU)} or {list(NO_DEPLOY_MENU)}")
        if self.verdict != "NO_VERDICT" and self.verdict not in self.menu:
            raise ValueError("the verdict must be on the menu offered")
        if (self.verdict == "DEPLOY") != (self.narrative is not None):
            raise ValueError("a DEPLOY carries every narrative field; no other verdict carries one")
        if (self.kind == "RENEWAL") != (self.renewal_of_version is not None):
            raise ValueError("renewal_of_version is set exactly for a RENEWAL")
        if self.renewal_of_version is not None:
            _matching(DIGEST, "renewal_of_version", self.renewal_of_version)
        return self

    def decided_at_utc(self) -> dt.datetime:
        return parse_aware(self.decided_at)

    def digest_body(self) -> dict:
        """The body the trader hashes and stores: times in UTC, so a respelled retry is the same judgment."""
        body = self.model_dump()
        body["decided_at"] = self.decided_at_utc().isoformat()
        return body
```

- [ ] **Step 5: Write the store** (`trader/automation/backtest_judgments.py`)

```python
"""Durable Jev judgments and the strategy-key cooldown (SP2c spec 5.2 items 2-3, 6.1).

``ai_research`` records one judgment per evaluation case. The trader is the
authority: it verifies the signed case itself, offers DEPLOY only when the case
qualifies (rules first), keeps one judgment per case and per evaluation, and
starts the strategy key's cooldown on REJECT. Rows are sealed: every read
recomputes the record digest. File reads, signature checks and the renewal
port run before the write transaction.
"""
from __future__ import annotations

import datetime as dt
import hmac
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional, Protocol

from exchange_calendars.errors import DateOutOfBounds

from trader.automation.backtest_judge_config import BacktestJudgeConfig
from trader.automation.backtest_judge_wire import RecordBacktestJudgmentRequest
from trader.automation.evaluation_claims import claim_row_in_tx, ny_day, utc
from trader.research.canonical import canonical_json_bytes, sha256_digest
from trader.research.evaluation_case import (
    CaseRefused, EvaluationCase, initial_deploy_allowed, load_case_verify_keys, load_verified_case, offered_menu,
    renewal_forward_complete,
)
from trader.research.strategy_key import split_strategy_key

JUDGMENT_DOMAIN = "mmr.backtest-judgment.v1"
JUDGMENT_BODY_DOMAIN = "mmr.backtest-judgment-body.v1"
MAX_DECIDED_AHEAD = dt.timedelta(minutes=5)
JUDGMENT_CONFLICT = "JUDGMENT_CONFLICT"
COOLDOWN_CALENDAR_UNAVAILABLE = "COOLDOWN_CALENDAR_UNAVAILABLE"
_COLUMNS = ("judgment_id, case_digest, request_id, kind, verdict, strategy_key, body_json, body_digest, "
            "binding_json, cooldown_until_session, recorded_at, record_digest")


class JudgmentRefused(Exception):
    def __init__(self, code: str, detail: str, *, retryable: bool = False):
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail
        self.retryable = retryable

    def reply(self, judgment_id: str) -> dict:
        return {"status": "REFUSED", "judgment_id": judgment_id, "code": self.code, "detail": self.detail,
                "retryable": self.retryable, "verdict": None, "cooldown_until_session": None}


@dataclass(frozen=True)
class RenewalStatus:
    refusal_code: Optional[str] = None       # refuse the whole judgment (unknown version, other binding)
    deploy_block_code: Optional[str] = None  # refuse only a DEPLOY (bundle expired, line ended)
    detail: str = ""


class RenewalChecks(Protocol):
    def status(self, case: EvaluationCase, *, now: dt.datetime) -> RenewalStatus: ...


class NoRenewalVersions:
    """Plans 1-4 judge no renewal; SP2c Plan 5 (Renewal) supplies the real checks."""

    def status(self, case: EvaluationCase, *, now: dt.datetime) -> RenewalStatus:
        return RenewalStatus(refusal_code="RENEWAL_NOT_SUPPORTED", detail="renewal arrives with SP2c Plan 5")


def nth_session_after(calendar: Any, day: dt.date, sessions: int) -> dt.date:
    """The ``sessions``-th XNYS session strictly after ``day``."""
    if sessions < 1:
        raise ValueError("sessions must be >= 1")
    try:
        found = calendar.sessions_in_range(day + dt.timedelta(days=1), day + dt.timedelta(days=2 * sessions + 14))
    except DateOutOfBounds as exc:
        raise JudgmentRefused(COOLDOWN_CALENDAR_UNAVAILABLE, str(exc)) from None
    if len(found) < sessions:
        raise JudgmentRefused(COOLDOWN_CALENDAR_UNAVAILABLE,
                              f"the calendar has fewer than {sessions} sessions after {day.isoformat()}")
    return found[sessions - 1]


@dataclass(frozen=True)
class BacktestJudgment:
    judgment_id: str
    case_digest: str
    request_id: Optional[str]
    kind: str
    verdict: str
    strategy_key: str
    body: dict
    binding: dict
    cooldown_until_session: Optional[dt.date]
    recorded_at: dt.datetime

    def to_json(self) -> dict:
        return {"judgment_id": self.judgment_id, "case_digest": self.case_digest, "request_id": self.request_id,
                "kind": self.kind, "verdict": self.verdict, "strategy_key": self.strategy_key, "body": self.body,
                "binding": self.binding, "cooldown_until_session": _iso_or_none(self.cooldown_until_session),
                "recorded_at": utc(self.recorded_at).isoformat()}


def _iso_or_none(day: Optional[dt.date]) -> Optional[str]:
    return None if day is None else day.isoformat()


def _text(value: Any) -> str:
    return canonical_json_bytes(value).decode("utf-8")


def record_digest(judgment: BacktestJudgment, body_digest: str) -> str:
    sealed = {**judgment.to_json(), "body_digest": body_digest}
    return "sha256:" + sha256_digest(JUDGMENT_DOMAIN, sealed)


def _binding(case: EvaluationCase, digest: str) -> dict:
    path, class_name = split_strategy_key(case.strategy_key)
    return {"case_digest": digest, "kind": case.kind, "stage": case.stage, "request_id": case.request_id,
            "strategy_key": case.strategy_key, "strategy_path": path, "class_name": class_name,
            "strategy_file_hash": case.strategy_file_hash, "params": case.selected_params,
            "conids": list(case.conids), "bar_size": case.bar_size, "family_id": case.family_id,
            "selected_trial_id": case.selected_trial_id, "artifact_id": case.artifact_id,
            "eligibility_decision_digest": case.eligibility_decision_digest,
            "prior_deployment_version": None if case.renewal is None else case.renewal.prior_deployment_version}


def _receipt(status: str, judgment_id: str, verdict: str, cooldown: Optional[dt.date]) -> dict:
    return {"status": status, "judgment_id": judgment_id, "code": None, "detail": None, "retryable": False,
            "verdict": verdict, "cooldown_until_session": _iso_or_none(cooldown)}


class BacktestJudgments:
    def __init__(self, db: Any, *, config: BacktestJudgeConfig, calendar: Any, cases_dir: Path, verify_dir: Path,
                 now: Callable[[], dt.datetime], renewals: Optional[RenewalChecks] = None):
        self._db = db
        self._config = config
        self._calendar = calendar
        self._cases_dir = Path(cases_dir)
        self._verify_dir = Path(verify_dir)
        self._now = now
        self._renewals = renewals if renewals is not None else NoRenewalVersions()

    def record(self, request: RecordBacktestJudgmentRequest) -> dict:
        body = request.digest_body()
        body_digest = sha256_digest(JUDGMENT_BODY_DOMAIN, body)
        try:
            prior = self._existing_reply(request.judgment_id, body_digest)
            if prior is not None:
                return prior
            now = utc(self._now())
            case = self._verified_case(request.case_digest)
            self._check_against_case(request, case, now)
            cooldown = self._cooldown_until(request.verdict, now)
            judgment = BacktestJudgment(request.judgment_id, request.case_digest, case.request_id, case.kind,
                                        request.verdict, case.strategy_key, body,
                                        _binding(case, request.case_digest), cooldown, now)
            return self._db.transaction(
                lambda conn: self._insert_in_tx(conn, judgment, case, body_digest, request.decided_at_utc()))
        except JudgmentRefused as refused:
            return refused.reply(request.judgment_id)

    def get(self, judgment_id: str) -> Optional[BacktestJudgment]:
        row = self._db.execute(f"SELECT {_COLUMNS} FROM backtest_judgments WHERE judgment_id = ?",
                               [judgment_id], fetch="one")
        if row is None:
            return None
        judgment = BacktestJudgment(row[0], row[1], row[2], row[3], row[4], row[5], json.loads(row[6]),
                                    json.loads(row[8]), row[9], utc(row[10]))
        if not hmac.compare_digest(record_digest(judgment, row[7]), row[11]):
            raise JudgmentRefused("JUDGMENT_TAMPERED", f"judgment {judgment_id} does not match its record digest")
        return judgment

    def get_by_case(self, case_digest: str) -> Optional[BacktestJudgment]:
        """One judgment per case (UNIQUE case_digest), so the case names at most one row."""
        row = self._db.execute("SELECT judgment_id FROM backtest_judgments WHERE case_digest = ?",
                               [case_digest], fetch="one")
        return None if row is None else self.get(row[0])

    def _existing_reply(self, judgment_id: str, body_digest: str) -> Optional[dict]:
        """A retry returns the first receipt without re-reading the case file."""
        row = self._db.execute("SELECT body_digest FROM backtest_judgments WHERE judgment_id = ?",
                               [judgment_id], fetch="one")
        if row is None:
            return None
        if not hmac.compare_digest(row[0], body_digest):
            raise JudgmentRefused(JUDGMENT_CONFLICT, "this judgment id already holds another body")
        stored = self.get(judgment_id)
        return _receipt("EXISTING", judgment_id, stored.verdict, stored.cooldown_until_session)

    def _verified_case(self, digest: str) -> EvaluationCase:
        try:
            return load_verified_case(self._cases_dir, digest, load_case_verify_keys(self._verify_dir))
        except CaseRefused as refused:
            raise JudgmentRefused(refused.code, refused.detail) from None

    def _check_against_case(self, request: RecordBacktestJudgmentRequest, case: EvaluationCase,
                            now: dt.datetime) -> None:
        if request.kind != case.kind:
            raise JudgmentRefused("JUDGMENT_KIND_MISMATCH", f"a {request.kind} judgment of a {case.kind} case")
        if request.decided_at_utc() > now + MAX_DECIDED_AHEAD:
            raise JudgmentRefused("DECIDED_IN_FUTURE", "decided_at is ahead of the trader clock")
        if tuple(request.menu) != offered_menu(case):
            raise JudgmentRefused("JUDGMENT_MENU_MISMATCH", f"the case offers {list(offered_menu(case))}")
        if case.kind == "RENEWAL":
            self._check_renewal(request, case, now)
        elif request.verdict == "DEPLOY" and not initial_deploy_allowed(case):
            raise JudgmentRefused("DEPLOY_NOT_ALLOWED", "only a complete case with every paper-v1 rule passed")

    def _check_renewal(self, request: RecordBacktestJudgmentRequest, case: EvaluationCase,
                       now: dt.datetime) -> None:
        if request.renewal_of_version != case.renewal.prior_deployment_version:
            raise JudgmentRefused("RENEWAL_VERSION_MISMATCH", "the judgment and the case name different versions")
        status = self._renewals.status(case, now=now)
        if status.refusal_code is not None:
            raise JudgmentRefused(status.refusal_code, status.detail)
        if request.verdict == "DEPLOY" and status.deploy_block_code is not None:
            raise JudgmentRefused(status.deploy_block_code, status.detail)
        if request.verdict == "DEPLOY" and not renewal_forward_complete(case):
            raise JudgmentRefused("DEPLOY_NOT_ALLOWED", "the forward evidence is incomplete")

    def _cooldown_until(self, verdict: str, now: dt.datetime) -> Optional[dt.date]:
        if verdict != "REJECT":
            return None
        return nth_session_after(self._calendar, ny_day(now), self._config.family_cooldown_sessions)

    def _insert_in_tx(self, conn: Any, judgment: BacktestJudgment, case: EvaluationCase, body_digest: str,
                      decided_at: dt.datetime) -> dict:
        row = conn.execute("SELECT body_digest, verdict, cooldown_until_session FROM backtest_judgments "
                           "WHERE judgment_id = ?", [judgment.judgment_id]).fetchone()
        if row is not None:                         # a concurrent retry won the race
            if not hmac.compare_digest(row[0], body_digest):
                raise JudgmentRefused(JUDGMENT_CONFLICT, "this judgment id already holds another body")
            return _receipt("EXISTING", judgment.judgment_id, row[1], row[2])
        other = conn.execute("SELECT judgment_id FROM backtest_judgments WHERE case_digest = ?",
                             [judgment.case_digest]).fetchone()
        if other is not None:
            raise JudgmentRefused(JUDGMENT_CONFLICT, f"the case is already judged by {other[0]}")
        if judgment.kind == "INITIAL":
            other = conn.execute("SELECT judgment_id FROM backtest_judgments WHERE request_id = ?",
                                 [judgment.request_id]).fetchone()
            if other is not None:
                raise JudgmentRefused(JUDGMENT_CONFLICT, f"the evaluation is already judged by {other[0]}")
            self._check_claim_in_tx(conn, case, decided_at)
        conn.execute(f"INSERT INTO backtest_judgments ({_COLUMNS}) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                     [judgment.judgment_id, judgment.case_digest, judgment.request_id, judgment.kind,
                      judgment.verdict, judgment.strategy_key, _text(judgment.body), body_digest,
                      _text(judgment.binding), judgment.cooldown_until_session, judgment.recorded_at,
                      record_digest(judgment, body_digest)])
        return _receipt("RECORDED", judgment.judgment_id, judgment.verdict, judgment.cooldown_until_session)

    @staticmethod
    def _check_claim_in_tx(conn: Any, case: EvaluationCase, decided_at: dt.datetime) -> None:
        claim = claim_row_in_tx(conn, case.request_id)
        if claim is None:
            raise JudgmentRefused("CASE_CLAIM_UNKNOWN", "the trader holds no claim for this evaluation")
        if claim.state not in ("DONE", "FAILED"):
            raise JudgmentRefused("CASE_CLAIM_NOT_FINISHED", f"the evaluation is {claim.state}", retryable=True)
        claimed = claim.body()
        same = (claim.strategy_key == case.strategy_key and claim.ny_day.isoformat() == case.claim_day
                and _text([claimed.cohort, claimed.conids, claimed.bar_size])
                == _text([case.cohort, case.conids, case.bar_size]))
        if not same:
            raise JudgmentRefused("CASE_CLAIM_MISMATCH", "the case is not the claimed evaluation")
        if decided_at < claim.claimed_at:
            raise JudgmentRefused("DECIDED_BEFORE_CLAIM", "decided_at is before the evaluation was claimed")
```

- [ ] **Step 6: Run the tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/automation/test_backtest_judgments.py tests/automation/test_evaluation_claims.py -q`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add trader/automation/backtest_judge_wire.py trader/automation/backtest_judgments.py \
  tests/automation/backtest_judge_fixtures.py tests/automation/test_backtest_judgments.py
git commit -m "$(cat <<'EOF'
feat: record sealed backtest judgments and start the cooldown on reject

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 5: The `research` principal and the six allow-list rows

**Files:**
- Modify: `trader/messaging/principals.py`, `docker-compose.yml` (trader volumes), `AGENTS.md` and `docs/ARCHITECTURE.md` (the principal lists)
- Modify tests: `tests/test_rpc_keys.py` (`test_peers_follow_the_trust_matrix`), `tests/test_rpc_keys_init.py` (rotation map), `tests/test_ai_policy_operator.py` (`test_ai_research_rights_are_exactly_the_sp1_set`)
- Test: `tests/test_research_principal.py` (new)

**Interfaces:**
- Consumes: `peers_for`, `rpc_files_for`, `RESTART_ON_ROTATE` (derived; no change to `rpc_keys.py`).
- Produces: the principal and ACL changes listed under "Principals" in Cross-plan additions.

- [ ] **Step 1: Write the failing tests** (`tests/test_research_principal.py`)

```python
"""SP2c Plan 1 Task 5: research joins only as a caller of the trader; Plan 3 makes it a service."""
from __future__ import annotations

from trader.messaging.principals import (
    CALLS, CLIENT_PRINCIPALS, KNOWN_PRINCIPALS, SERVER_ACCEPTS, SERVER_PRINCIPALS, SERVICE_PRINCIPAL, TRADER_ACL,
    rpc_files_for,
)
from trader.messaging.rpc_keys import RESTART_ON_ROTATE

BACKTEST_JUDGE_RIGHTS = {
    ("command", "claim_evaluation"): {"research"},
    ("query", "get_evaluation_claim"): {"research"},
    ("command", "update_evaluation_claim"): {"research"},
    ("query", "get_deployment_forward_evidence"): {"research"},
    ("command", "record_backtest_judgment"): {"ai_research"},
    ("query", "get_backtest_judgment"): {"research", "ai_research", "cli", "dashboard"},
}


def test_plan_1_adds_research_only_as_a_caller_of_the_trader():
    assert "research" in KNOWN_PRINCIPALS and CALLS["research"] == {"trader"}
    assert "research" in SERVER_ACCEPTS["trader"]
    assert "research" not in SERVER_PRINCIPALS and "research" not in SERVER_ACCEPTS
    assert "research" not in CLIENT_PRINCIPALS
    assert "research" not in SERVICE_PRINCIPAL and "research" not in SERVICE_PRINCIPAL.values()
    assert not [caller for caller, callees in CALLS.items() if caller != "research" and "research" in callees]
    assert RESTART_ON_ROTATE["research"] == ("trader",)              # Plan 3: ("ai", "research", "trader")
    assert "research.pub" in rpc_files_for("trader")


def test_backtest_judge_rights_are_exact():
    assert {key: set(TRADER_ACL[key]) for key in BACKTEST_JUDGE_RIGHTS} == BACKTEST_JUDGE_RIGHTS


def test_research_has_no_trading_policy_or_decision_right():                       # review focus 5
    rights = {key for key, allowed in TRADER_ACL.items() if "research" in allowed}
    assert rights == {key for key, allowed in BACKTEST_JUDGE_RIGHTS.items() if "research" in allowed}
```

In `tests/test_rpc_keys.py::test_peers_follow_the_trust_matrix` change the trader line to:

```python
    assert principals.peers_for("trader") == {"cli", "dashboard", "strategy", "ai_supervisor", "ai_research",
                                              "research"}
    assert principals.peers_for("research") == {"trader"}
```

In `tests/test_rpc_keys_init.py`, next to the existing `RESTART_ON_ROTATE` asserts, add:

```python
    assert RESTART_ON_ROTATE["research"] == ("trader",)        # SP2c Plan 1; Plan 3 adds ai and research
```

In `tests/test_ai_policy_operator.py::test_ai_research_rights_are_exactly_the_sp1_set` the exact `ai_research` rights gain the two judgment methods:

```python
        ("command", "register_ai_deployment"), ("query", "get_ai_deployment"),
        ("command", "record_backtest_judgment"), ("query", "get_backtest_judgment")}     # SP2c Plan 1
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_research_principal.py tests/test_rpc_keys.py tests/test_rpc_keys_init.py -q`
Expected: FAIL with `assert 'research' in frozenset({...})` (and `KeyError: 'research'` in the rotation map).

- [ ] **Step 3: Write the implementation**

`trader/messaging/principals.py`:

```python
KNOWN_PRINCIPALS: frozenset[str] = frozenset({
    "trader", "strategy", "cli", "dashboard", "ai_supervisor", "ai_research", "research",
})
```

```python
SERVER_ACCEPTS: Mapping[str, frozenset[str]] = {
    "trader": frozenset({"cli", "dashboard", "strategy", "ai_supervisor", "ai_research", "research"}),
    "strategy": frozenset({"cli", "dashboard", "trader"}),
}
```

In `CALLS` add (comment included, it records the split with Plan 3):

```python
    # SP2c Plan 1: research only calls the trader. Plan 3 makes it a server (SERVER_PRINCIPALS,
    # SERVER_ACCEPTS["research"], SERVICE_PRINCIPAL) and lets ai_research and cli call it.
    "research": frozenset({"trader"}),
```

In `TRADER_ACL`, after the SP2 Plan 2 rows:

```python
    # SP2c Plan 1 (spec 5.1 table): evaluation claims and judgments. Explicit sets per method.
    ("command", "claim_evaluation"): frozenset({"research"}),
    ("query", "get_evaluation_claim"): frozenset({"research"}),
    ("command", "update_evaluation_claim"): frozenset({"research"}),
    ("query", "get_deployment_forward_evidence"): frozenset({"research"}),
    ("command", "record_backtest_judgment"): frozenset({"ai_research"}),
    ("query", "get_backtest_judgment"): frozenset({"research", "ai_research", "cli", "dashboard"}),
```

`docker-compose.yml`, `trader` service, after the `strategy.pub` bind:

```yaml
      - ${HOME}/.config/mmr/keys/rpc/research.pub:/home/trader/.config/mmr/keys/rpc/research.pub:ro
```

`AGENTS.md` (Architecture paragraph) and `docs/ARCHITECTURE.md` ("Typed Ed25519 RPC"): in the principal list `(`trader`, `strategy`, `cli`, `dashboard`, `ai_supervisor`, `ai_research`)` add `, `research` (SP2c: calls the trader only for evaluation claims; its own service and ports come with SP2c Plan 3)`. Plan 3 adds the ports 42106/42107 to the port tables.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_research_principal.py tests/test_rpc_keys.py tests/test_rpc_keys_init.py tests/test_compose_rpc_keys.py tests/test_rpc_key_rotation.py tests/test_rpc_acl.py tests/test_keys_check_mount.py tests/test_rpc_keys_backup.py tests/test_ai_policy_operator.py -q`
Expected: PASS. (`test_each_service_sees_only_its_own_key_pair_and_needed_public_keys[trader]` needs the new bind; the rotation test now also covers `research`.)

- [ ] **Step 5: Commit**

```bash
git add trader/messaging/principals.py docker-compose.yml AGENTS.md docs/ARCHITECTURE.md \
  tests/test_research_principal.py tests/test_rpc_keys.py tests/test_rpc_keys_init.py tests/test_ai_policy_operator.py
git commit -m "$(cat <<'EOF'
feat: add the research principal as a caller of the trader

The trader container now binds research.pub, so run ./docker.sh -k before
deploying; docker.sh refuses to start while the key file is missing.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 6: The RPC surface, the forward-evidence port and the wiring

**Files:**
- Create: `trader/automation/forward_evidence.py`, `trader/messaging/backtest_judge_surface.py`
- Modify: `trader/automation/backtest_judge_wire.py` (five request models), `trader/messaging/production_api.py` (`register_ai_paper_authority`), `trader/trading/command_stack.py` (`AiPaperServices`, migrations, `_build_ai_paper_services`)
- Test: `tests/automation/test_backtest_judge_surface.py` (new), `tests/test_ai_paper_rpc.py` (two tests added)

**Interfaces:**
- Consumes: `EvaluationClaims`, `ClaimRefused` (Task 3); `BacktestJudgments`, `JudgmentRefused` (Task 4); `TypedRpcRegistry.register(role, method, request_model, response_model, handler, *, execution, with_caller)`, `RpcCaller`, `_DispatchProblem` (`trader/messaging/typed_rpc.py`); `ServedStack`, `make_identities`, `build_full_production_registry`, `ALLOW_ALL` (`tests/rpc_identity_fixtures.py`).
- Produces: `register_backtest_judge_surface(registry, *, claims, judgments, forward_evidence) -> None`; `ForwardEvidenceSource`, `ForwardEvidenceRefused`, `NoDeploymentVersions`; `AiPaperServices.claims`, `.judgments`, `.forward_evidence`; the six methods on the real trader registry.

- [ ] **Step 1: Write the failing tests** (`tests/automation/test_backtest_judge_surface.py`)

```python
"""SP2c Plan 1 Task 6: the six methods over the real signed trader server (spec 5.1 table, 9)."""
from __future__ import annotations

import threading
import time

import pytest

from tests.automation.backtest_judge_fixtures import (
    ZERO, count_claims, finished, judge_config, judgment, request_body, world,
)
from tests.rpc_identity_fixtures import ALLOW_ALL, ServedStack, build_full_production_registry, make_identities
from trader.automation.evaluation_claims import EvaluationClaims
from trader.automation.forward_evidence import NoDeploymentVersions
from trader.data.duckdb_store import DuckDBConnection
from trader.messaging.backtest_judge_surface import register_backtest_judge_surface
from trader.messaging.principals import SERVER_ACCEPTS, TRADER_ACL
from trader.messaging.typed_rpc import TypedRpcRegistry, TypedRpcRemoteError
from trader.research.evaluation_request import evaluation_request_id

ROWS = {
    ("command", "claim_evaluation"): {"research"},
    ("query", "get_evaluation_claim"): {"research"},
    ("command", "update_evaluation_claim"): {"research"},
    ("query", "get_deployment_forward_evidence"): {"research"},
    ("command", "record_backtest_judgment"): {"ai_research"},
    ("query", "get_backtest_judgment"): {"research", "ai_research", "cli", "dashboard"},
}


def claim_body(body=None) -> dict:
    body = body or request_body()
    return {"request_id": evaluation_request_id(body), "body": body.model_dump()}


VALID_BODIES = {
    "claim_evaluation": claim_body,
    "get_evaluation_claim": lambda: {"request_id": ZERO},
    "update_evaluation_claim": lambda: {"request_id": ZERO, "state": "RUNNING"},
    "get_deployment_forward_evidence": lambda: {"deployment_version": ZERO},
    "record_backtest_judgment": lambda: judgment(ZERO, "SHADOW").model_dump(),
    "get_backtest_judgment": lambda: {"judgment_id": "jdg-00000001", "case_digest": None},
}


class SlowFirstReply:
    """The claim commits, then the first reply is late: the caller times out after acceptance."""

    def __init__(self, claims, delay: float):
        self._claims, self._delay, self._late = claims, delay, True

    def claim(self, *args, **kwargs):
        result = self._claims.claim(*args, **kwargs)
        if self._late:
            self._late = False
            time.sleep(self._delay)
        return result

    def __getattr__(self, name):
        return getattr(self._claims, name)


def serve(w, *, claims=None, acl=TRADER_ACL) -> ServedStack:
    registry = TypedRpcRegistry(acl=acl, default_execution="thread")
    register_backtest_judge_surface(registry, claims=claims or w.claims, judgments=w.judgments,
                                    forward_evidence=NoDeploymentVersions())
    return ServedStack({("trader", "command"): registry, ("trader", "query"): registry}, make_identities())


@pytest.fixture
def w(tmp_path):
    return world(tmp_path)


@pytest.fixture
def served(w):
    stack = serve(w)
    yield stack
    stack.close()


def test_every_method_is_refused_for_every_caller_outside_its_row(served):            # review focus 5
    for (role, method), allowed in ROWS.items():
        assert TRADER_ACL[(role, method)] == allowed
        for principal in sorted(SERVER_ACCEPTS["trader"] - allowed):
            with pytest.raises(TypedRpcRemoteError) as exc:
                served.client(principal, role=role).call(method, {}, dict)
            assert exc.value.code == "PERMISSION_DENIED", (principal, method)


def test_each_handler_refuses_a_wrong_caller_even_behind_an_open_allow_list(w):      # review focus 5
    stack = serve(w, acl=ALLOW_ALL)
    try:
        for (role, method), allowed in ROWS.items():
            for principal in sorted(SERVER_ACCEPTS["trader"] - allowed):
                with pytest.raises(TypedRpcRemoteError) as exc:
                    stack.client(principal, role=role).call(method, VALID_BODIES[method](), dict)
                assert exc.value.code == "PERMISSION_DENIED", (principal, method)
    finally:
        stack.close()


def test_research_claims_reads_back_and_moves_the_claim_forward(served):
    command, query = served.client("research", role="command"), served.client("research", role="query")
    reply = command.call("claim_evaluation", claim_body(), dict)
    assert (reply["status"], reply["claim"]["state"], reply["claim"]["ny_day"]) == ("ACCEPTED", "QUEUED", "2026-10-08")
    request_id = reply["claim"]["request_id"]
    assert command.call("update_evaluation_claim", {"request_id": request_id, "state": "RUNNING"}, dict)["status"] \
        == "UPDATED"
    assert query.call("get_evaluation_claim", {"request_id": request_id}, dict)["claim"]["state"] == "RUNNING"
    assert command.call("update_evaluation_claim", {"request_id": request_id, "state": "RUNNING"}, dict)["status"] \
        == "UNCHANGED"


def test_a_conflict_is_a_refused_reply_not_an_rpc_error(served):
    command = served.client("research", role="command")
    command.call("claim_evaluation", claim_body(), dict)
    other = {**claim_body(), "body": request_body(bar_size="1 min").model_dump()}
    reply = command.call("claim_evaluation", other, dict)
    assert (reply["status"], reply["code"], reply["claim"]) == ("REFUSED", "EVALUATION_REQUEST_CONFLICT", None)


def test_two_concurrent_claims_for_the_last_slot_over_rpc_accept_exactly_one(tmp_path):    # review focus 1
    w = world(tmp_path, evaluations_per_day=2)
    stack = serve(w)
    try:
        stack.client("research", role="command").call(
            "claim_evaluation", claim_body(request_body(research_day="2026-10-01")), dict)
        clients = [stack.client("research", role="command") for _ in range(2)]
        bodies = [claim_body(request_body(research_day=day)) for day in ("2026-10-02", "2026-10-03")]
        barrier, outcomes = threading.Barrier(2), []

        def attempt(client, body):
            barrier.wait()
            reply = client.call("claim_evaluation", body, dict)
            outcomes.append(reply["code"] or reply["status"])

        threads = [threading.Thread(target=attempt, args=pair) for pair in zip(clients, bodies)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)
        assert sorted(outcomes) == ["ACCEPTED", "EVALUATION_LIMIT_REACHED"] and count_claims(w.db) == 2
    finally:
        stack.close()


def test_a_lost_reply_after_acceptance_is_read_back_and_never_takes_a_second_slot(tmp_path):    # review focus 2
    w = world(tmp_path, evaluations_per_day=1)
    stack = serve(w, claims=SlowFirstReply(w.claims, delay=1.5))
    try:
        body = claim_body()
        with pytest.raises(TimeoutError):
            stack.client("research", role="command", timeout=0.5).call("claim_evaluation", body, dict)
        found = stack.client("research", role="query").call("get_evaluation_claim",
                                                            {"request_id": body["request_id"]}, dict)
        assert found["found"] and found["claim"]["state"] == "QUEUED"
        retry = stack.client("research", role="command").call("claim_evaluation", body, dict)
        assert (retry["status"], retry["claim"]["request_id"]) == ("EXISTING", body["request_id"])
        assert count_claims(w.db) == 1
    finally:
        stack.close()


def test_a_restarted_trader_serves_the_same_claim(tmp_path, w):
    body = claim_body()
    first = serve(w)
    try:
        first.client("research", role="command").call("claim_evaluation", body, dict)
    finally:
        first.close()
    reopened = EvaluationClaims(DuckDBConnection(str(tmp_path / "journal.duckdb")), config=judge_config(), now=w.clock)
    second = serve(w, claims=reopened)
    try:
        found = second.client("research", role="query").call("get_evaluation_claim",
                                                              {"request_id": body["request_id"]}, dict)
        assert found["claim"]["state"] == "QUEUED"
        assert second.client("research", role="command").call("claim_evaluation", body, dict)["status"] == "EXISTING"
    finally:
        second.close()


def test_ai_research_records_and_every_reader_reads_the_judgment(served, w):
    reply = served.client("ai_research", role="command").call(
        "record_backtest_judgment", judgment(finished(w), "DEPLOY").model_dump(), dict)
    assert reply["status"] == "RECORDED"
    for reader in ("research", "ai_research", "cli", "dashboard"):
        got = served.client(reader, role="query").call(
            "get_backtest_judgment", {"judgment_id": "jdg-00000001", "case_digest": None}, dict)
        assert got["found"] and got["judgment"]["verdict"] == "DEPLOY"
        assert got["judgment"]["body"]["jev_model"] == "openrouter/jev-1"


def test_a_judgment_is_read_by_exactly_one_of_its_id_or_its_case(served, w):          # Plan 3 shadow discovery
    case = finished(w)
    served.client("ai_research", role="command").call(
        "record_backtest_judgment", judgment(case, "SHADOW").model_dump(), dict)
    reader = served.client("research", role="query")
    by_case = reader.call("get_backtest_judgment", {"judgment_id": None, "case_digest": case}, dict)
    assert by_case["found"] and by_case["judgment"]["judgment_id"] == "jdg-00000001"
    unknown = reader.call("get_backtest_judgment", {"judgment_id": None, "case_digest": "sha256:" + "e" * 64}, dict)
    assert unknown == {"found": False, "judgment": None}
    for both_or_none in ({"judgment_id": "jdg-00000001", "case_digest": case},
                         {"judgment_id": None, "case_digest": None}):
        with pytest.raises(TypedRpcRemoteError) as exc:
            reader.call("get_backtest_judgment", both_or_none, dict)
        assert exc.value.code == "VALIDATION_ERROR"


def test_a_tampered_judgment_is_an_rpc_error_not_a_missing_row(served, w):
    served.client("ai_research", role="command").call(
        "record_backtest_judgment", judgment(finished(w), "SHADOW").model_dump(), dict)
    w.db.execute("UPDATE backtest_judgments SET strategy_key = 'strategies/x.py:X' WHERE judgment_id = 'jdg-00000001'")
    with pytest.raises(TypedRpcRemoteError) as exc:
        served.client("cli", role="query").call("get_backtest_judgment",
                                                {"judgment_id": "jdg-00000001", "case_digest": None}, dict)
    assert exc.value.code == "JUDGMENT_TAMPERED"


def test_forward_evidence_is_refused_until_versions_exist(served):
    reply = served.client("research", role="query").call(
        "get_deployment_forward_evidence", {"deployment_version": ZERO}, dict)
    assert (reply["status"], reply["code"], reply["evidence"]) == ("REFUSED", "DEPLOYMENT_VERSION_UNKNOWN", None)


def test_the_full_production_registry_registers_the_six_methods():
    registered = {(r.socket_role, r.method) for r in build_full_production_registry().registrations()}
    assert set(ROWS) <= registered
```

Add to `tests/test_ai_paper_rpc.py` (uses its `served`, `served_disabled`, `command`, `query` helpers):

```python
def test_backtest_judge_methods_serve_on_the_real_paper_stack(served):               # SP2c Plan 1
    from trader.data.schema_migrations import SchemaMigrator
    from trader.research.evaluation_request import EvaluationRequestBody, evaluation_request_id

    zero = "sha256:" + "0" * 64
    assert query(served, "research").call("get_evaluation_claim", {"request_id": zero}, dict) == \
        {"found": False, "claim": None}
    body = {"strategy_key": "strategies/opening_range_breakout.py:OpeningRangeBreakout",
            "cohort": [{"RANGE_MINUTES": 15}], "conids": [265598], "bar_size": "5 mins", "research_day": "2026-10-08"}
    request_id = evaluation_request_id(EvaluationRequestBody.model_validate(body))
    reply = command(served, "research").call("claim_evaluation", {"request_id": request_id, "body": body}, dict)
    assert (reply["status"], reply["code"]) == ("REFUSED", "STRATEGY_NOT_ALLOWED")   # the default allowlist is empty
    assert {110, 111} <= SchemaMigrator(served.trader.journal_db).applied_versions()


def test_backtest_judge_methods_are_absent_when_ai_paper_is_off(served_disabled):    # SP2c Plan 1
    with pytest.raises(TypedRpcRemoteError) as exc:
        query(served_disabled, "research").call("get_evaluation_claim", {"request_id": "sha256:" + "0" * 64}, dict)
    assert exc.value.code == "METHOD_NOT_ALLOWED"
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/automation/test_backtest_judge_surface.py tests/test_ai_paper_rpc.py::test_backtest_judge_methods_serve_on_the_real_paper_stack tests/test_ai_paper_rpc.py::test_backtest_judge_methods_are_absent_when_ai_paper_is_off -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'trader.automation.forward_evidence'`.

- [ ] **Step 3: Write the request models** (append to `trader/automation/backtest_judge_wire.py`)

```python
from pydantic import field_validator

from trader.research.evaluation_request import REQUEST_ID, EvaluationRequestBody


class ClaimEvaluationRequest(_Strict):
    request_id: str
    body: EvaluationRequestBody

    @field_validator("request_id")
    @classmethod
    def _request_id(cls, value: str) -> str:
        return _matching(REQUEST_ID, "request_id", value)


class GetEvaluationClaimRequest(_Strict):
    request_id: str

    @field_validator("request_id")
    @classmethod
    def _request_id(cls, value: str) -> str:
        return _matching(REQUEST_ID, "request_id", value)


class UpdateEvaluationClaimRequest(_Strict):
    request_id: str
    state: Literal["RUNNING", "DONE", "FAILED"]

    @field_validator("request_id")
    @classmethod
    def _request_id(cls, value: str) -> str:
        return _matching(REQUEST_ID, "request_id", value)


class GetBacktestJudgmentRequest(_Strict):
    """Exactly one key is set: the judgment id, or the case digest (one judgment per case)."""
    judgment_id: Optional[str]
    case_digest: Optional[str]

    @model_validator(mode="after")
    def _one_key(self) -> "GetBacktestJudgmentRequest":
        if (self.judgment_id is None) == (self.case_digest is None):
            raise ValueError("set exactly one of judgment_id or case_digest")
        if self.judgment_id is not None:
            _matching(JUDGMENT_ID, "judgment_id", self.judgment_id)
        else:
            _matching(DIGEST, "case_digest", self.case_digest)
        return self


class GetDeploymentForwardEvidenceRequest(_Strict):
    deployment_version: str

    @field_validator("deployment_version")
    @classmethod
    def _version(cls, value: str) -> str:
        return _matching(DIGEST, "deployment_version", value)
```

(Move the two new imports to the top of the module with the others.)

- [ ] **Step 4: Write the port and the surface**

`trader/automation/forward_evidence.py`:

```python
"""``get_deployment_forward_evidence`` (SP2c spec 5.2 item 7): a port that SP2c Plan 5 fills.

Plan 5 reads Plan 2's version, its sessions and its paper trips, and Plan 3's
shadow rows. Until then the default refuses every read.
"""
from __future__ import annotations

from typing import Protocol


class ForwardEvidenceRefused(Exception):
    def __init__(self, code: str, detail: str):
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


class ForwardEvidenceSource(Protocol):
    def read(self, deployment_version: str) -> dict: ...


class NoDeploymentVersions:
    def read(self, deployment_version: str) -> dict:
        raise ForwardEvidenceRefused("DEPLOYMENT_VERSION_UNKNOWN", "forward evidence arrives with SP2c Plan 5")
```

`trader/messaging/backtest_judge_surface.py`:

```python
"""SP2c backtest-judge methods on the trader (spec 5.1 table).

Direct handlers: no command ledger, no controller epoch. The allow-list is not the
authority; each handler checks its caller again. A business refusal is a reply
body; a tampered judgment is an RPC error.
"""
from __future__ import annotations

from typing import Any

from trader.automation.backtest_judge_wire import (
    ClaimEvaluationRequest, GetBacktestJudgmentRequest, GetDeploymentForwardEvidenceRequest,
    GetEvaluationClaimRequest, RecordBacktestJudgmentRequest, UpdateEvaluationClaimRequest,
)
from trader.automation.backtest_judgments import JudgmentRefused
from trader.automation.evaluation_claims import ClaimRefused
from trader.automation.forward_evidence import ForwardEvidenceRefused
from trader.messaging.typed_rpc import RpcCaller, _DispatchProblem

RESEARCH = frozenset({"research"})
AI_RESEARCH = frozenset({"ai_research"})
JUDGMENT_READERS = frozenset({"research", "ai_research", "cli", "dashboard"})


def _require(caller: RpcCaller, allowed: frozenset[str], method: str) -> None:
    if caller.principal not in allowed:
        raise _DispatchProblem("PERMISSION_DENIED", f"principal {caller.principal!r} may not call {method!r}")


def register_backtest_judge_surface(registry: Any, *, claims: Any, judgments: Any, forward_evidence: Any) -> None:
    def claim_evaluation(parsed: ClaimEvaluationRequest, caller: RpcCaller) -> dict:
        _require(caller, RESEARCH, "claim_evaluation")
        try:
            return claims.claim(parsed.request_id, parsed.body, principal=caller.principal).reply()
        except ClaimRefused as refused:
            return refused.reply()

    def get_evaluation_claim(parsed: GetEvaluationClaimRequest, caller: RpcCaller) -> dict:
        _require(caller, RESEARCH, "get_evaluation_claim")
        claim = claims.get(parsed.request_id)
        return {"found": claim is not None, "claim": None if claim is None else claim.to_json()}

    def update_evaluation_claim(parsed: UpdateEvaluationClaimRequest, caller: RpcCaller) -> dict:
        _require(caller, RESEARCH, "update_evaluation_claim")
        try:
            return claims.update(parsed.request_id, parsed.state).reply()
        except ClaimRefused as refused:
            return refused.reply()

    def record_backtest_judgment(parsed: RecordBacktestJudgmentRequest, caller: RpcCaller) -> dict:
        _require(caller, AI_RESEARCH, "record_backtest_judgment")
        return judgments.record(parsed)

    def get_backtest_judgment(parsed: GetBacktestJudgmentRequest, caller: RpcCaller) -> dict:
        _require(caller, JUDGMENT_READERS, "get_backtest_judgment")
        try:
            judgment = (judgments.get(parsed.judgment_id) if parsed.judgment_id is not None
                        else judgments.get_by_case(parsed.case_digest))
        except JudgmentRefused as refused:          # a tampered row fails loudly, never reads as missing
            raise _DispatchProblem(refused.code, refused.detail) from None
        return {"found": judgment is not None, "judgment": None if judgment is None else judgment.to_json()}

    def get_deployment_forward_evidence(parsed: GetDeploymentForwardEvidenceRequest, caller: RpcCaller) -> dict:
        _require(caller, RESEARCH, "get_deployment_forward_evidence")
        try:
            evidence = forward_evidence.read(parsed.deployment_version)
        except ForwardEvidenceRefused as refused:
            return {"status": "REFUSED", "code": refused.code, "detail": refused.detail, "evidence": None}
        return {"status": "FOUND", "code": None, "detail": None, "evidence": evidence}

    registry.register("command", "claim_evaluation", ClaimEvaluationRequest, dict, claim_evaluation,
                      execution="thread", with_caller=True)
    registry.register("query", "get_evaluation_claim", GetEvaluationClaimRequest, dict, get_evaluation_claim,
                      execution="thread", with_caller=True)
    registry.register("command", "update_evaluation_claim", UpdateEvaluationClaimRequest, dict,
                      update_evaluation_claim, execution="thread", with_caller=True)
    registry.register("command", "record_backtest_judgment", RecordBacktestJudgmentRequest, dict,
                      record_backtest_judgment, execution="thread", with_caller=True)
    registry.register("query", "get_backtest_judgment", GetBacktestJudgmentRequest, dict, get_backtest_judgment,
                      execution="thread", with_caller=True)
    registry.register("query", "get_deployment_forward_evidence", GetDeploymentForwardEvidenceRequest, dict,
                      get_deployment_forward_evidence, execution="thread", with_caller=True)
```

- [ ] **Step 5: Wire it into the trader**

`trader/trading/command_stack.py`:

1. `AiPaperServices` gains, after `baseline_sizer`:

```python
    claims: Any = None            # EvaluationClaims (SP2c Plan 1)
    judgments: Any = None         # BacktestJudgments (SP2c Plan 1)
    forward_evidence: Any = None  # ForwardEvidenceSource (SP2c Plan 1 default; Plan 5 replaces it)
```

2. Next to `apply_scope_check_migration(migrator)  # 100`:

```python
    from trader.automation.backtest_judge_schema import apply_backtest_judge_migrations

    apply_backtest_judge_migrations(migrator)         # 110, 111 (SP2c Plan 1)
```

3. In `_build_ai_paper_services`, before the `return AiPaperServices(...)`:

```python
    from trader.automation.backtest_judgments import BacktestJudgments
    from trader.automation.calendar_policy import XNYSCalendarPolicy
    from trader.automation.evaluation_claims import EvaluationClaims
    from trader.automation.forward_evidence import NoDeploymentVersions
    from trader.research.evaluation_case import default_cases_dir, default_verify_dir

    judge_config = parts.config.backtest_judge
    claims = EvaluationClaims(trader.journal_db, config=judge_config, now=now)
    judgments = BacktestJudgments(trader.journal_db, config=judge_config, calendar=XNYSCalendarPolicy(),
                                  cases_dir=default_cases_dir(), verify_dir=default_verify_dir(), now=now)
```

and pass `claims=claims, judgments=judgments, forward_evidence=NoDeploymentVersions()` to `AiPaperServices(...)`.

`trader/messaging/production_api.py`: at the end of `register_ai_paper_authority` call `_register_backtest_judge(registry, ai_paper)` and add:

```python
def _register_backtest_judge(registry: TypedRpcRegistry, ai_paper) -> None:
    """SP2c Plan 1: only on an enabled ai_paper stack, which is paper only."""
    from trader.messaging.backtest_judge_surface import register_backtest_judge_surface
    if getattr(ai_paper, "claims", None) is None:
        return
    register_backtest_judge_surface(registry, claims=ai_paper.claims, judgments=ai_paper.judgments,
                                    forward_evidence=ai_paper.forward_evidence)
```

- [ ] **Step 6: Run the tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/automation/test_backtest_judge_surface.py tests/test_ai_paper_rpc.py tests/test_rpc_acl.py tests/test_command_stack.py -q`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add trader/automation/forward_evidence.py trader/automation/backtest_judge_wire.py \
  trader/messaging/backtest_judge_surface.py trader/messaging/production_api.py trader/trading/command_stack.py \
  tests/automation/test_backtest_judge_surface.py tests/test_ai_paper_rpc.py
git commit -m "$(cat <<'EOF'
feat: serve evaluation claims and backtest judgments over signed rpc

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 7: Full suite

**Files:** none new. Fix only failures this plan caused.

- [ ] **Step 1: Run every test file of this plan together**

Run: `.venv/bin/python -m pytest tests/automation/test_backtest_judge_config.py tests/research/test_evaluation_request_and_case.py tests/automation/test_evaluation_claims.py tests/automation/test_backtest_judgments.py tests/test_research_principal.py tests/automation/test_backtest_judge_surface.py tests/test_ai_paper_rpc.py -q`
Expected: PASS.

- [ ] **Step 2: Run the full suite once, under the shared lock**

```bash
until mkdir /private/tmp/mmr-suite.lock 2>/dev/null; do sleep 30; done
MMR_STRATEGIES_EXTRA_ROOT=/private/tmp/pytest-sp2c-p1 .venv/bin/python -m pytest tests/ -q -n 8 --timeout=120 \
  --ignore=tests/test_ibrx_async.py -p no:cacheprovider --basetemp=/private/tmp/pytest-sp2c-p1; status=$?
chmod -R u+w /private/tmp/pytest-sp2c-p1; rm -rf /private/tmp/pytest-sp2c-p1
rmdir /private/tmp/mmr-suite.lock
exit $status
```

Expected: all pass. If pytest-xdist is missing, run single-process with `--timeout=60` instead of `-n 8 --timeout=120`. Then run `.venv/bin/python -m pytest tests/test_ibrx_async.py --timeout=30 -q` alone.

A dry run of this plan's code on a copy of master (2026-10-08) left no fallout beyond the edits in Task 5. If something new appears (a test pinning `peers_for("trader")`, the trader's compose key files, `RESTART_ON_ROTATE` or exact principal rights), fix it in the task that caused it, never by loosening an assertion to a subset.

- [ ] **Step 3: Commit any fallout fix**

```bash
git add -A tests/
git commit -m "$(cat <<'EOF'
test: align key and rotation expectations with the research principal

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
EOF
)"
```

(Skip this step when nothing needed fixing.)

- [ ] **Step 4: Note for the PR description**

The PR body must say: "Before deploying: run `./docker.sh -k` (creates `research.key` / `research.pub`), then `./docker.sh -b -u`. The trader refuses to start without `research.pub`. `ai_paper.backtest_judge.strategy_allowlist` is empty by default, so every claim is refused until the operator lists strategies. RENEWAL judgments and the forward-evidence read are refused until SP2c Plan 5 (Renewal)." No container restart or deploy is done by this plan.
