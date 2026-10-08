# AI Paper SP2c — Plan 3: research service: evaluations, evaluation cases, attestation, shadow replay — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add the `research` service. It owns the evaluation pipeline for AI-proposed candidates: it claims a slot at the trader before any work, runs a frozen cohort through the full pre-holdout gate, opens one holdout for the code-selected point, signs a non-authorizing evaluation case for every finished or failed evaluation, turns a durable DEPLOY judgment into the `llm` review and a signed bundle (paper only), and replays every judged strategy each night so the scoreboard shows one shadow book per verdict.

**Architecture:** A new typed RPC server principal `research` (42106 query, 42107 command) runs in its own container (`trader/research_service.py`). Request checks, the cohort evaluator, the case signer, the attest handoff and the shadow replay are plain modules under `trader/research/`; the service wires them to one worker thread and a trader port (signed typed RPC clients toward the trader). The evaluator change splits today's `evaluate` into pre-holdout per cohort point, a code selection, and one holdout. The backtester gains a `trading_start` option for warm-up without trading. The trader gains one command, `record_shadow_result`, a sealed `shadow_results` table (journal migration 120) and shadow books in the scoreboard report.

**Tech Stack:** Python 3.12, DuckDB (`SchemaMigrator`, `DuckDBConnection.transaction`), pydantic v2 strict wire models, Ed25519 (`cryptography`), `exchange_calendars`, pandas, pytest. No new dependencies.

**Spec:** `docs/superpowers/specs/2026-10-08-ai-paper-sp2c-backtest-judge-design.md` sections 5.1, 6.2, 7, 8, 9 (and 3 for the words). Index: `docs/superpowers/plans/2026-10-08-ai-paper-sp2c-00-index.md`.

## Global Constraints

- **Base:** master after SP2c Plan 1 (claims, judgments, config, the request and case modules) and Plan 2 (deployment versions). Task 9 touches no Plan 1 name and can start first; every other task needs Plan 1's modules or its `research` principal entries; Task 10 also needs Plan 2's `version_for_judgment`. Task 11's shadow discovery reads Plan 1's `get_backtest_judgment` by `case_digest`.
- **Migrations.** Trader journal: **120** (`shadow_results`) in `trader/scoreboard/schema.py`; 121–124 stay unused. Research DB: **20** (`research_requests`), **21** (`research_cases`), **22** (`shadow_members`, `shadow_sent`, `shadow_failures`) in the research schema's own style (`migrator.apply(version=..., name=..., statements=...)`), called from `apply_research_migrations`. No ALTER, no backfill (owner: no legacy data).
- **Principals.** Plan 1 adds a minimal `research` entry for its trader methods. This plan extends it to the full spec 5.1 identity. Keep one copy of every entry; never re-add what Plan 1 already wrote.
- **Wire models:** `ConfigDict(extra="forbid", strict=True)`. Ids are regex-checked. Refusals are reply bodies (`status: "REFUSED"`, `code`, `detail`), never raised errors (a raised error becomes a scrubbed `INTERNAL_ERROR`).
- **Every handler re-checks its principal** (`with_caller=True`), as `ai_paper_actions.py` does. The allow-list is not the authority; the handler is.
- **DuckDB:** only through `DuckDBConnection.transaction` / `execute`. Inside a transaction callback never call `db.execute` or `db.transaction`.
- **Signing:** the research signing key is loaded with `AttestationSigner.from_key_file` (never generated in the service) and never logged. Cases are Plan 1's `EvaluationCase`, written with Plan 1's `write_evaluation_case` under its domain tag `mmr.research.evaluation-case.v1`; this plan defines no second case or request format.
- **Paper only.** `attest_from_judgment` refuses on anything but a paper posture. No IB connection, no orders, no model calls in this service.
- **Fail loudly:** missing bars, wrong-size bars, unknown conids, a changed strategy file → a refusal or an INCOMPLETE row with a reason. Nothing is guessed.
- Test-first. Per task run only the listed tests. Full suite once, in Task 12.
- Commit subjects `feat:` / `test:` / `refactor:` / `docs:`, lowercase, imperative. Every commit ends with a blank line and `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.
- No container restart, broker order, deploy or push is authorized by this plan.

## Rulings

1. **The service sets `research_day`.** Plan 1's claim body (`EvaluationRequestBody`) carries `research_day`, and the request id is the digest of that body. The `ai` caller sends only `kind`, `strategy_key`, `cohort`, `conids`, `bar_size` (Plan 4); the research service adds `research_day` = today's New York date before it computes the id. So the caller chooses neither the id nor the day, and the same candidate can be evaluated again on a later day. The period is derived from `research_day` and frozen in the stored body. *Cost if wrong:* a retry across New York midnight is a new request and takes a new slot (rare: the cycle runs after the close).
2. **Neighbours are code-derived:** for each numeric tunable named in a point, ±10 % (int: step `max(1, round(|v|·0.1))`; float: `v·0.9` and `v·1.1`, rounded to 6 significant digits; bool and str: none). A point without a numeric tunable is refused (`COHORT_POINT_INVALID`). The request has no neighbourhood field. *Cost:* a different robustness neighbourhood than an expert would pick; it is fixed and never model-chosen.
3. **Evaluation defaults** live in a new top-level `research_service:` block of `trader.yaml` (spec 11 open question: reuse `research/example_spec.yaml`): `period_sessions: 690`, `folds: 6`, `embargo_sessions: 5`, `holdout_sessions: 90`, `order_notional: 1900`, `account_equity: 100000`, `max_gross_allocation: 0.05`, `queue_max: 20`, `shadow_incomplete_after_hours: 16`. A separate block, so Plan 1's strict `ai_paper.backtest_judge` loader never meets unknown keys.
4. **"Conids in scope"** means: Plan 1's `check_conids` (1–20, strictly increasing), at least `MIN_INSTRUMENTS` (8), each priced by `execution_costs.yaml` on one venue with the XNYS calendar (the existing spec checks). Live discretionary scope (quotes, volume) stays the trader's check at the ENTER. *Cost:* an evaluated name may still be refused at entry.
5. **Selection key** is `selection_adjusted_confidence` (the code's deflated walk-forward statistic, `evidence.selection_confidence`), highest wins, ties by cohort order. The spec says "deflated walk-forward Sharpe"; this is the value the gate already computes.
6. **Holdout availability:** the new holdout must start strictly after the latest end of every holdout already opened for the strategy key. Checked before the claim (`HOLDOUT_NOT_AVAILABLE`) and again just before opening. The registry's overlap guard stays as a backstop.
7. **Replay point:** `evidence.replay_index` is the selected point; with no selection, the point with the highest selection statistic regardless of pass (ties by cohort order; none computable → 0). Fixed when the case is signed.
8. **`NO_VERDICT` judgments do not join the shadow cohort** (there is no verdict book for them).
9. **Shadow books are global**, not per experiment: a judgment belongs to no experiment. The report gains a top-level `shadow_books`.
10. **Shadow row identity is resolved by the trader:** research sends `judgment_id` and `session_date`; the trader reads the judgment's deployment version (Plan 2 `version_for_judgment`, one version per judgment) once, at the first insert, and stores it. `record_id = "sha256:" + sha256_digest("mmr.shadow-result.v1", {judgment_id, session_date})`. A judgment has at most one version, so (judgment, version or none, session) and (judgment, session) name the same row; keying on the pair stops a second row when the registration lands between two sends.
11. **An INCOMPLETE shadow row waits** until `shadow_incomplete_after_hours` (16 h) after the session close before it is recorded, so a late bar download does not freeze a row as INCOMPLETE (rows are immutable).
12. **Paper posture for attest:** env `TRADING_MODE` is `paper`, `trader.yaml` `trading_mode` (when set) is `paper`, and `IB_ACCOUNT` starts with `DU`. Any disagreement → `ACCOUNT_NOT_PAPER`. The trader re-checks at registration (Plan 2).
13. **DB volume (owner to confirm).** `research` mounts `mmr_db_data` read-write, as `strategy` does. A `:ro` mount breaks bar reads (connections here open read-write and the stores run `CREATE ... IF NOT EXISTS`), and podman has no volume `subpath`. Its own research DB lives on a new volume `mmr_research_data` (`MMR_RESEARCH_DUCKDB`). The journal file is visible in that volume; no research module names it (test). *Cost if wrong:* read exposure of the journal; a bug could write the bar file. Follow-up option: a read-only DuckDB mode plus a split volume.
14. **Signing key only in `research`.** A tmpfs masks `~/.config/mmr/keys/private` in `data`, `trader`, `strategy`, `dashboard`, `scheduler`, `cli`; only `research` binds `keys/private/signing.pem` (read-only). `check-mount` enforces it. `docker compose run cli research attest bundle` stops working; host `mmr research attest bundle` still works. `./docker.sh -u` refuses when `signing.pem` is missing (a missing single-file bind becomes a directory).
15. **Trader `keys/verify` is bound read-only** over its read-write config mount (spec 5.1 mounts table).
16. **Service reports** go to `artifacts/reports`, summaries to `artifacts/evaluations`, cases to `artifacts/cases` (Plan 1's `default_cases_dir()`).
17. **Renewal cases are SP2c Plan 5 (Renewal).** This plan builds no RENEWAL case and no `ForwardEvidenceSource` (Plan 1 ruling 11's port; Plan 5 fills it over this plan's `shadow_results` and Plan 2's paper trips). `submit_evaluation` accepts Plan 4's `{"kind": "RENEWAL", "prior_version_digest"}` shape and refuses it with `RENEWAL_NOT_SUPPORTED`, the same code as Plan 1's default renewal port, so a caller sees a clear refusal. Plan 5 adds the RENEWAL case builder and the RENEWAL submit kind here. *Cost if wrong:* until Plan 5 an expired DEPLOY cannot be renewed; a new evaluation with a new holdout still works.
18. **Compose:** `research` has `profiles: ["ai"]`, `mem_limit: 2g`, `cpus: 1.0`, no host port.
19. **A runtime failure** after an accepted claim (bars unreadable, backtest error, changed file, the holdout re-check) signs a case with stage `FAILED` and a class-name summary in `evidence.error`, and moves the claim to `FAILED`. Terminal trials stay counted.
20. **One evaluation at a time**, queue bounded by `queue_max`; a full queue refuses `QUEUE_FULL` **before** the claim.
21. **Executing file hash** = `"sha256:" + sha256(exact file bytes)` at submit (`strategy_file_hash`); a different hash when the run starts → stage `FAILED`, `STRATEGY_SOURCE_CHANGED`.
22. **A HOLDOUT_FAILED case names the decision without recording it.** The evaluator records no eligibility decision for a failed holdout (a retired artifact is never attested), but Plan 1's case shape needs `eligibility_decision_digest` for that stage. The case carries `decision.digest` (computed, not stored). It can never be deployed (`initial_deploy_allowed` needs `COMPLETE` and `holdout_passed is True`).
23. **The holdout result goes into the case header** (Plan 1 ruling 15, PR #91 thread 4218218927). `build_initial_case` sets `holdout_passed` from the `HoldoutOutcome` it ran (`True` / `False`), `None` when no holdout was opened; `build_failed_case` sets `None`. Both write `evidence["holdout"]` from the same result (PR #91 OpenAI round 2): `holdout_evidence(outcome)` is `null` when no holdout was opened, else the window with `passed = bool(outcome.passed)`, the very value put in `holdout_passed`; `build_failed_case` writes `evidence["holdout"] = None`. Plan 1's model refuses a case whose evidence and header disagree (`CASE_MALFORMED`). `evaluation_summary` reports this header field, not the evidence copy, and `attest_from_judgment` requires it too.
24. **A finished evaluation is shown only after the trader confirmed its end** (PR #91 thread 4218218688). After the case is signed, the request row keeps `state = 'RUNNING'` and records the owed end in `pending_report` (`DONE` or `FAILED`). The report is sent at once and again on **every service tick** (`run_next`, also `recover`) until the trader answers `UPDATED` or `UNCHANGED`; after a lost reply the claim is read back with `get_evaluation_claim`, and a claim already in that state counts as confirmed. Only then does the row move to `DONE` / `FAILED` and `get_evaluation` show the case. Plan 4's pump treats Plan 1's `CASE_CLAIM_NOT_FINISHED` as retryable, which stays as the safety net. *Cost if wrong:* while the trader is unreachable a finished case waits (Plan 4's `evaluation_stale_hours` still bounds it).
25. **A shadow row is sent only when the trader stored it** (PR #91 thread 4218219293). `mark_sent` runs only after `record_shadow_result` answers `INSERTED` (the trader's word for accepted) or `DUPLICATE` (the trader answers `DUPLICATE` only when the stored body digest equals ours; another body is the refusal `CONFLICTING_DUPLICATE`). A retryable refusal (`JUDGMENT_UNKNOWN`) leaves the row pending for the next tick. Any other refusal is final for that session: the row goes to `shadow_failures` with the code and detail, is logged at ERROR, is counted by `ShadowReplay.status()["failed_rows"]` (logged at ERROR after each tick while not zero), and is never sent again or shown as sent; later sessions of the same judgment still go out (each row is its own change of one continuous run). The operator clears a failure by deleting its `shadow_failures` row after fixing the cause; the next tick sends it again. *Cost if wrong:* none to safety; a missing shadow session is visible, never a silent gap.

## Cross-plan additions

Plan 1 is the base: this plan uses its names exactly (`trader/research/evaluation_request.py`, `trader/research/evaluation_case.py`, `trader/research/strategy_key.py`, `trader/automation/backtest_judge_config.py`, the claim/judgment wire in `trader/automation/backtest_judge_wire.py`). It does **not** define its own case or request module.

- **From Plan 1 (its ruling R4):** `get_backtest_judgment` takes `{"judgment_id": str|null, "case_digest": sha256|null}` with exactly one set (one judgment per case, so the lookup is unique). Task 11's shadow discovery reads SHADOW and REJECT judgments by `case_digest`; no other spec 5.1 method tells the research service about them.
- **Consumed from Plan 1:** `EvaluationRequestBody`, `evaluation_request_id`, `check_conids`; `EvaluationCase`, `CASE_DOMAIN`, `write_evaluation_case(cases_dir, case, signer) -> digest`, `load_verified_case(cases_dir, digest, keys)`, `initial_deploy_allowed`, `offered_menu`, `FULL_MENU`, `CaseRefused`, `default_cases_dir()`; `split_strategy_key`; `BacktestJudgeConfig.allows`, `.max_cohort_points`, `.shadow_warmup_sessions`, `.deploy_expiry_sessions`, `.family_cooldown_sessions`; `load_backtest_judge_config(raw_ai_paper.get("backtest_judge"))`; claim replies (`ACCEPTED|EXISTING|REFUSED`, `ClaimView` with `ny_day`, `state`), `get_evaluation_claim → {found, claim}`, `update_evaluation_claim → UPDATED|UNCHANGED|REFUSED`; `get_backtest_judgment → {found, judgment: {judgment_id, case_digest, kind, verdict, body: {jev_model, decided_at, narrative, ...}, binding: {...}}}`; on the trader object `ai_paper.judgments` (`BacktestJudgments.get(id) -> BacktestJudgment|None`, raising `JudgmentRefused`) and `ai_paper_config.backtest_judge`.
- **Consumed from Plan 2:** `trader.ai_deployment_versions.version_for_judgment(judgment_id) -> Optional[str]`.
- **Produced by Plan 3 (Plans 1, 2, 4 use these):**
  - Research server: query **42106**, command **42107**; host `research`; `ai` binds `research.pub` (Task 2). `trader.research.research_surface.build_research_registry(*, evaluations, attest) -> TypedRpcRegistry` (one registry for both roles, as the trader does).
  - `submit_evaluation` request (strict): `{"kind": "INITIAL", "strategy_key", "cohort", "conids" (sorted), "bar_size"}` or `{"kind": "RENEWAL", "prior_version_digest"}` (refused `RENEWAL_NOT_SUPPORTED` until Plan 5, ruling 17). The caller sends no day: the service sets `research_day` (ruling 1). Reply: `{"status": "ACCEPTED"|"DUPLICATE"|"REFUSED", "request_id", "state", "code", "detail", "retryable"}`; `CLAIM_UNKNOWN` is retryable, a trader refusal keeps the trader's `retryable`; a same-day resend of an accepted body is `DUPLICATE` with the same `request_id`. Refusal codes include `FAMILY_COOLING_DOWN`, `EVALUATION_LIMIT_REACHED`, `EVALUATION_REQUEST_CONFLICT` (from the trader), `HOLDOUT_NOT_AVAILABLE`, `STRATEGY_NOT_ALLOWED`, `COHORT_TOO_LARGE`, `COHORT_POINT_INVALID`, `CONIDS_OUT_OF_SCOPE`, `REQUEST_INVALID`, `SPEC_INVALID`, `QUEUE_FULL`, `CLAIM_UNKNOWN`, `PRINCIPAL_FORBIDDEN`.
  - `get_evaluation` `{"request_id"}` → `{"found", "request_id", "state", "case_digest", "summary"}`. Until the trader has confirmed the claim's `DONE` or `FAILED` (ruling 24), the reply says `state: "RUNNING"` with `case_digest` and `summary` null, so a caller never judges a case whose claim the trader still holds open. `summary` = `evaluation_summary(case)` (Plan 4's `CaseSummary` models every key): `kind, strategy_key, strategy_path, class_name, file_hash, params, conids, bar_size, stage, rules_passed, holdout_passed, eligibility, renewal_checks_passed, prior_version_digest, order_notional, strategy_trials, prior_holdouts, previously_revealed_sessions, selected_index, error, metrics, points [{index, params, pre_holdout_passed, failed_rules, missing_rules, metrics {expectancy_bps_1x, expectancy_bps_1_5x, expectancy_bps_2x, selection_statistic}}], rule_results [{point, rule, passed}], forward`. `rules_passed` is `offered_menu(case) == FULL_MENU`, the same rule the trader's menu check uses. Every value is code-computed; never a bundle digest.
  - `attest_from_judgment` `{"judgment_id"}` → `{"status": "ATTESTED"|"DUPLICATE"|"REFUSED", "bundle_digest", "code", "detail", "retryable", "binding"}`. ATTESTED and DUPLICATE carry `binding` = `{strategy_path, class_name, file_hash, params, conids, bar_size, order_notional}` read from the bundle just signed (Plan 2's `binding_differences` facts); a refusal has `binding: null`. `TRADER_UNAVAILABLE` is retryable.
  - The case `evidence` dict (signed, read only by Plan 3 and humans): `points [{index, params, trial_id, neighbour_trial_ids, pre_holdout_passed, failed_rules, missing_rules, rules [{code, passed, observed}], expectancy_bps {1x, 1.5x, 2x}, selection_statistic}]`, `strategy_trials`, `selected_index`, `replay_index`, `holdout {start, end, passed, detail}|null` (always present; `passed` equals the header `holdout_passed`, ruling 23), `previously_revealed [ISO dates]`, `holdouts_opened_before`, `warmup_sessions`, `error`.
  - `trader/research/shadow_window.py`: `shadow_window(decided_at, verdict, *, deploy_expiry_sessions, family_cooldown_sessions) -> (first_session, last_session)`.
  - Trader command `record_shadow_result` (`research` only) and table `shadow_results` (journal migration 120): `record_id, judgment_id, deployment_version, session_date, verdict, case_digest, status, reason, pnl_usd, fees_usd, trades, end_equity_usd, bar_size, body_digest, recorded_at`; Plan 5's `ForwardEvidenceSource` reads it by `deployment_version`.
  - Plan 4's acceptance fixture `tests/research/research_service_fixtures.py::research_stack` depends on Plan 4's served harness, so Plan 4 writes it from Plan 3's public constructors (`EvaluationService`, `JudgmentAttest`, `build_research_registry`, `ResearchStore`, `TraderPort`).
- **Plan 2 note:** the case module is Plan 1's. Plan 2 reads it with `load_case_verify_keys` / `load_verified_case(...)` and the `EvaluationCase` header fields (`strategy_key`, `strategy_file_hash`, `selected_params`, `conids`, `bar_size`, `artifact_id`, `family_id`).

## Review Focus

1. A refused claim runs nothing and writes no trial; a lost reply is read back and takes one slot; a restart resumes `QUEUED`/`RUNNING` under the same claim; a lost `DONE` report withholds the case and is sent again on the next tick without a restart — `tests/research/test_evaluation_service.py` (`test_a_lost_terminal_report_withholds_the_case_until_the_next_tick_confirms_it`, `test_a_lost_terminal_reply_is_confirmed_by_reading_the_claim_back`); end to end in Plan 4 Task 8 `test_a_lost_terminal_claim_update_is_sent_again_and_the_judgment_records`.
2. Cohort selection and holdout: the best 1x point that fails 2x is not selected, a neighbour is never selected, no pass → no artifact and no holdout, a shifted window → `HOLDOUT_NOT_AVAILABLE` before any claim — `tests/research/test_cohort_evaluation.py`, `tests/research/test_cohort_request.py`.
3. A case never authorizes: the bundle verifier and `require_qualified_research_evidence` refuse a case the service wrote; a rule failure and a holdout failure are cases that offer only SHADOW / REJECT — `tests/research/test_case_builder.py`.
4. Review handoff: no durable DEPLOY, live, or another review → refused with no review row; a DEPLOY writes one `llm` review with code-set holdout confirmation; a repeat returns the same digest — `tests/research/test_judgment_attest.py`.
5. Access and shadow: every research method refused outside its row at the server, `research` calls no trading method, warm-up books no fill, a repeat row is a no-op and a changed one is refused, a refused row is never marked sent (`test_a_final_refusal_is_a_visible_failure_and_never_marked_sent`, `test_a_retryable_refusal_leaves_the_row_pending`), books per verdict with INCOMPLETE — `tests/research/test_research_surface.py`, `tests/test_research_principal.py`, `tests/test_backtester_trading_start.py`, `tests/scoreboard/test_shadow_results.py`, `tests/research/test_shadow_replay.py`.

## File map

| File | Change |
|---|---|
| `trader/messaging/principals.py`, `rpc_keys.py`, `typed_rpc.py`, `keys_cli.py` | full `research` identity, ACLs, rotation, signing-key mount rule |
| `docker-compose.yml`, `docker.sh` | `research` service, key masks, `-K` gate |
| `trader/research/service_config.py` (new) | `research_service` block |
| `trader/research/evaluation_spec.py` | `build_evaluation_spec` shared by YAML and RPC; public `declared_tunables` |
| `trader/research/cohort.py` (new) | request body, checks, neighbours, period, holdout availability |
| `trader/research/case_builder.py` (new) | Plan 1 `EvaluationCase` from a cohort result or a failure; `evaluation_summary` |
| `trader/research/evaluation.py` | extract `run_holdout`, `point_market_context`, `run_environment` (no behaviour change) |
| `trader/research/cohort_evaluation.py` (new) | pre-holdout per point, selection, one holdout |
| `trader/research/service_store.py` (new) | research migrations 20–22 |
| `trader/research/trader_port.py` (new) | signed calls to the trader, lost-reply readback |
| `trader/research/evaluation_service.py` (new) | submit, get, worker, restart recovery |
| `trader/research/judgment_attest.py` (new) | `attest_from_judgment` |
| `trader/research/research_surface.py` (new) | wire models and registry |
| `trader/research_service.py` (new) | process entry |
| `trader/simulation/backtester.py`, `trader/research/evaluation_jobs.py` | `trading_start` |
| `trader/research/shadow_window.py`, `trader/research/shadow_replay.py` (new) | shadow cohort and nightly replay |
| `trader/scoreboard/schema.py`, `store.py`, `shadow_ingest.py` (new), `books.py`, `report.py`, `service.py`; `trader/messaging/shadow_surface.py` (new); `production_api.py`, `command_stack.py` | `record_shadow_result`, shadow books |
| `config_defaults/trader.yaml`, `docs/ARCHITECTURE.md`, `AGENTS.md`, `docs/OPERATIONAL_STATE.md` | config block, docs |

### Task 1: The full `research` principal (server identity, ACLs, rotation)

**Files:** Modify `trader/messaging/principals.py`, `trader/messaging/rpc_keys.py`, `trader/messaging/typed_rpc.py`, and the tests Plan 1 wrote or edited: `tests/test_research_principal.py`, `tests/test_rpc_keys.py`, `tests/test_rpc_keys_init.py`.

**Interfaces.** Consumes Plan 1's entries (`KNOWN_PRINCIPALS` has `research`, `CALLS["research"] = {"trader"}`, `SERVER_ACCEPTS["trader"]` has `research`, the six backtest-judge `TRADER_ACL` rows). Produces `SERVER_PRINCIPALS ∋ research`, `SERVER_ACCEPTS["research"] = {"ai_research", "cli"}`, `CALLS["ai_research"]` and `CALLS["cli"]` gaining `research`, `principals.RESEARCH_ACL`, `TRADER_ACL[("command", "record_shadow_result")] = {"research"}`, `SERVICE_PRINCIPAL["research"] = "research"`, `RESTART_ON_ROTATE["research"] == ("ai", "research", "trader")`.

- [ ] **Step 1: Failing tests.** In `tests/test_research_principal.py`, replace Plan 1's `test_plan_1_adds_research_only_as_a_caller_of_the_trader` and `test_research_has_no_trading_policy_or_decision_right` with the tests below, and import `RESEARCH_ACL`, `service_rpc_files` too. Keep `BACKTEST_JUDGE_RIGHTS` and `test_backtest_judge_rights_are_exact`.

```python
from tests.rpc_identity_fixtures import make_identities
from trader.messaging.principals import RESEARCH_ACL, service_rpc_files
from trader.messaging.typed_rpc import TypedRpcClient, TypedRpcRegistry, TypedRpcServer


def test_research_is_a_server_that_calls_the_trader_and_not_an_sdk_signer():
    assert "research" in KNOWN_PRINCIPALS and "research" in SERVER_PRINCIPALS
    assert "research" not in CLIENT_PRINCIPALS
    assert SERVER_ACCEPTS["research"] == {"ai_research", "cli"}
    assert CALLS["research"] == {"trader"}
    assert {"research", "trader"} <= CALLS["ai_research"] and "research" in CALLS["cli"]
    assert "research" in SERVER_ACCEPTS["trader"]


def test_research_acl_is_the_spec_table():
    assert {key: set(value) for key, value in RESEARCH_ACL.items()} == {
        ("command", "submit_evaluation"): {"ai_research"},
        ("query", "get_evaluation"): {"ai_research", "cli"},
        ("command", "attest_from_judgment"): {"ai_research"}}
    assert all(allowed <= SERVER_ACCEPTS["research"] for allowed in RESEARCH_ACL.values())


def test_research_has_no_trading_policy_or_decision_right():                       # review focus 5
    rights = {key for key, allowed in TRADER_ACL.items() if "research" in allowed}
    expected = {key for key, allowed in BACKTEST_JUDGE_RIGHTS.items() if "research" in allowed}
    assert rights == expected | {("command", "record_shadow_result")}
    assert TRADER_ACL[("command", "record_shadow_result")] == {"research"}


def test_service_identity_and_key_files():
    assert SERVICE_PRINCIPAL["research"] == "research"
    assert service_rpc_files("research") == {
        "research.key", "research.pub", "trader.pub", "ai_research.pub", "cli.pub"}
    for service in ("ai", "trader", "cli"):
        assert "research.pub" in service_rpc_files(service)


def test_rotation_restarts_follow_the_new_peers():
    assert RESTART_ON_ROTATE["research"] == ("ai", "research", "trader")
    assert RESTART_ON_ROTATE["cli"] == ("research", "strategy", "trader")
    assert RESTART_ON_ROTATE["ai_research"] == ("ai", "research", "trader")
    assert RESTART_ON_ROTATE["trader"] == ("ai", "dashboard", "research", "strategy", "trader")


def test_typed_server_and_client_accept_research():
    identities = make_identities()
    server = TypedRpcServer("query", TypedRpcRegistry(acl=RESEARCH_ACL), identities["research"], port=0)
    client = TypedRpcClient("query", identities["ai_research"], server="research", port=1)
    assert server.identity.principal == "research" and client.server == "research"
```

In `tests/test_rpc_keys.py::test_peers_follow_the_trust_matrix` two lines change: Plan 1's research line becomes `assert principals.peers_for("research") == {"trader", "ai_research", "cli"}` and `assert principals.peers_for("ai_research") == {"trader", "research"}` (the trader line already has `research` from Plan 1). `tests/test_rpc_key_rotation.py` needs no edit: its `_start_server` is generic and now also starts a `research` server, and the `cli` client trusts `research.pub`. In `tests/test_rpc_keys_init.py` the rotation asserts become `RESTART_ON_ROTATE["cli"] == ("research", "strategy", "trader")`, `RESTART_ON_ROTATE["ai_research"] == ("ai", "research", "trader")`, `RESTART_ON_ROTATE["research"] == ("ai", "research", "trader")`.

- [ ] **Step 2: Run** `.venv/bin/python -m pytest tests/test_research_principal.py tests/test_rpc_keys.py tests/test_rpc_keys_init.py -q` — expected: FAIL (`assert 'research' in frozenset({'trader', 'strategy'})`).

- [ ] **Step 3: Implement.** In `principals.py` (Plan 1's lines stay; one copy of each entry):

```python
SERVER_PRINCIPALS: frozenset[str] = frozenset({"trader", "strategy", "research"})

SERVER_ACCEPTS: Mapping[str, frozenset[str]] = {
    "trader": frozenset({"cli", "dashboard", "strategy", "ai_supervisor", "ai_research", "research"}),
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
}
```

In `TRADER_ACL`, next to Plan 1's backtest-judge rows:

```python
    # SP2c Plan 3 (spec 7): only the research service records shadow rows.
    ("command", "record_shadow_result"): frozenset({"research"}),
```

After `STRATEGY_ACL`:

```python
# SP2c spec 5.1: the research server's methods. Each handler re-checks its caller.
RESEARCH_ACL: Mapping[tuple[str, str], frozenset[str]] = {
    ("command", "submit_evaluation"): frozenset({"ai_research"}),
    ("query", "get_evaluation"): frozenset({"ai_research", "cli"}),
    ("command", "attest_from_judgment"): frozenset({"ai_research"}),
}
```

`SERVICE_PRINCIPAL` gains `"research": "research"`. In `rpc_keys.py`:

```python
_LONG_LIVED_SERVICE_PRINCIPALS: Mapping[str, tuple[str, ...]] = {
    "trader": ("trader",), "strategy": ("strategy",), "dashboard": ("dashboard",),
    "ai": ("ai_supervisor", "ai_research"), "research": ("research",),
}
```

In `typed_rpc.py` `TypedRpcServer.__init__` the message becomes `f"a typed RPC server needs a server identity (one of {sorted(SERVER_PRINCIPALS)})"`.

- [ ] **Step 4: Run** `.venv/bin/python -m pytest tests/test_research_principal.py tests/test_rpc_keys.py tests/test_rpc_keys_init.py tests/test_rpc_acl.py tests/test_rpc_key_rotation.py tests/test_rpc_keys_backup.py -q` — expected: PASS.

- [ ] **Step 5: Commit** `feat: make research a typed rpc server principal`

```
feat: make research a typed rpc server principal

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
```

### Task 2: Compose service, key masks and the `-K` gate

**Files:** Modify `docker-compose.yml`, `docker.sh`, `trader/messaging/keys_cli.py`, `tests/test_docker_helper.py` (`KEYCHECK_SERVICES` gains `"research"`; `FakeDocker.write_keys` also writes the host signing key), `tests/test_keys_check_mount.py` (`_container_view` gives `research` its signing key), `tests/test_compose_ai_service.py` (the ai key set gains `"research.pub"`). Create `tests/test_compose_research_service.py`.

**Interfaces.** Produces `keys_cli.SIGNING_KEY_HOLDERS = frozenset({"research"})` and `keys_cli.signing_key_problems(service, private_dir) -> list[str]`.

- [ ] **Step 1: Failing test** `tests/test_compose_research_service.py`

```python
from pathlib import Path

import pytest

from tests.compose_rpc_helpers import CONTAINER_CONFIG, ROOT, load_compose, visible_rpc_files, volumes
from trader.messaging.keys_cli import signing_key_problems
from trader.messaging.principals import service_rpc_files

PRIVATE_DIR = f"{CONTAINER_CONFIG}/keys/private"
CONFIG_MOUNTERS = ("data", "trader", "strategy", "dashboard", "scheduler", "cli")


@pytest.fixture(scope="module")
def services():
    return load_compose()["services"]


def test_research_sees_its_keys_and_no_credentials(services):
    research = services["research"]
    assert visible_rpc_files(research) == service_rpc_files("research")
    env = research["environment"]
    assert not [k for k in env if k.startswith(("ALPACA_", "OPENROUTER", "AWS_", "AZURE_", "MASSIVE_", "TELEGRAM"))]
    assert research["profiles"] == ["ai"] and not research.get("ports")
    assert research["command"] == ["python", "-m", "trader.research_service"]


def test_only_research_mounts_the_signing_key(services):
    for name, svc in services.items():
        binds = [v for v in volumes(svc) if v["target"].startswith(PRIVATE_DIR + "/")]
        if name == "research":
            assert [(v["target"], v["read_only"]) for v in binds] == [(f"{PRIVATE_DIR}/signing.pem", True)]
        else:
            assert not binds, name
    for name in CONFIG_MOUNTERS:
        assert any(v["type"] == "tmpfs" and v["target"] == PRIVATE_DIR for v in volumes(services[name])), name


def test_research_mounts(services):
    targets = {v["target"]: v for v in volumes(services["research"])}
    for name in ("trader.yaml", "execution_costs.yaml"):
        assert targets[f"{CONTAINER_CONFIG}/{name}"]["read_only"]
    assert targets["/home/trader/.local/share/mmr/research"]["source"] == "mmr_research_data"
    assert targets["/home/trader/.local/share/mmr/data"]["source"] == "mmr_db_data"     # ruling 13
    assert not targets["/home/trader/.local/share/mmr/artifacts"]["read_only"]
    assert not [v for v in volumes(services["research"]) if "secrets" in v["source"]]


def test_trader_verify_dir_is_read_only_and_ai_sees_research_pub(services):
    verify = [v for v in volumes(services["trader"]) if v["target"] == f"{CONTAINER_CONFIG}/keys/verify"]
    assert verify and verify[0]["read_only"]
    assert "research.pub" in visible_rpc_files(services["ai"])


def test_keycheck_and_startup_gate_cover_research():
    text = (ROOT / "docker.sh").read_text()
    assert 'KEYCHECK_SERVICES="trader strategy dashboard cli scheduler data ai research"' in text
    assert "_compose_private_key_files" in text and 'volume rm "${KEYCHECK_PROJECT}_mmr_research_data"' in text


def test_research_service_never_names_the_journal():
    for path in (ROOT / "trader" / "research_service.py", *sorted((ROOT / "trader" / "research").glob("*.py"))):
        assert "journal_duckdb_path" not in path.read_text(), path


def test_signing_key_rule(tmp_path: Path):
    (tmp_path / "signing.pem").write_text("x")
    assert signing_key_problems("research", tmp_path) == []
    assert signing_key_problems("trader", tmp_path) == [f"unexpected {tmp_path / 'signing.pem'}"]
    assert signing_key_problems("research", tmp_path / "missing") == [f"missing {tmp_path / 'missing' / 'signing.pem'}"]
```

(`test_research_service_never_names_the_journal` passes only after Task 8 creates `trader/research_service.py`; until then it is skipped by `pytest.importorskip("trader.research_service")` at its first line — add that line.)

Existing test helpers that the new rule touches:

```python
# tests/test_docker_helper.py, FakeDocker.write_keys: ./docker.sh -u and -K now need the host signing key.
    def write_keys(self) -> None:
        write_keyset(self.rpc_dir)
        private = self.home / ".config" / "mmr" / "keys" / "private"
        private.mkdir(parents=True, exist_ok=True)
        (private / "signing.pem").write_text("test signing key placeholder\n")
```

```python
# tests/test_keys_check_mount.py, _container_view, after the rpc copies: only research sees the signing key.
    if service == "research":
        (tmp_path / "private").mkdir(exist_ok=True)
        (tmp_path / "private" / "signing.pem").write_text("x")


def test_a_signing_key_outside_research_fails_the_gate(tmp_path):
    argv = _container_view(tmp_path, "trader")
    (tmp_path / "private").mkdir()
    (tmp_path / "private" / "signing.pem").write_text("x")
    code, out = _run(argv)
    assert code == 1 and "unexpected" in out and "signing.pem" in out
```

- [ ] **Step 2: Run** `.venv/bin/python -m pytest tests/test_compose_research_service.py tests/test_keys_check_mount.py -q` — expected: FAIL (`KeyError: 'research'`, `ImportError: signing_key_problems`).

- [ ] **Step 3: Implement.**

`docker-compose.yml` — after the `ai` service:

```yaml
  # ── research: evaluations, cases, attestation, shadow replay (SP2c Plan 3) ──
  # Typed RPC server 42106 (query) / 42107 (command) on the private network only.
  # Holds the research signing key (read-only) and no other private key, no IB or
  # model credentials, no Telegram secrets. mmr_db_data like strategy (ruling 13);
  # its own research DB on mmr_research_data.
  research:
    <<: *mmr-hardening
    build: *mmr-build
    profiles: ["ai"]
    working_dir: /home/trader/mmr
    command: ["python", "-m", "trader.research_service"]
    depends_on:
      - trader
    mem_limit: 2g
    cpus: 1.0
    environment:
      TZ: ${TIME_ZONE:-America/New_York}
      PYTHONDONTWRITEBYTECODE: "1"
      MMR_CONFIG_DEFAULTS: "off"
      TRADER_CONFIG: /home/trader/.config/mmr/trader.yaml
      TRADING_MODE: ${TRADING_MODE:-paper}
      IB_ACCOUNT: ${IB_ACCOUNT:-}
      TRADER_TYPED_ADDRESS: tcp://trader
      RESEARCH_TYPED_BIND_ADDRESS: tcp://0.0.0.0
      MMR_RESEARCH_DUCKDB: /home/trader/.local/share/mmr/research/mmr_research.duckdb
    volumes:
      - type: tmpfs
        target: /home/trader/.config/mmr
        tmpfs:
          size: 65536
          mode: 0755
      - type: tmpfs
        target: /home/trader/.config/mmr/keys/rpc
        tmpfs:
          size: 65536
          mode: 0755
      - ${HOME}/.config/mmr/keys/rpc/research.key:/home/trader/.config/mmr/keys/rpc/research.key:ro
      - ${HOME}/.config/mmr/keys/rpc/research.pub:/home/trader/.config/mmr/keys/rpc/research.pub:ro
      - ${HOME}/.config/mmr/keys/rpc/trader.pub:/home/trader/.config/mmr/keys/rpc/trader.pub:ro
      - ${HOME}/.config/mmr/keys/rpc/ai_research.pub:/home/trader/.config/mmr/keys/rpc/ai_research.pub:ro
      - ${HOME}/.config/mmr/keys/rpc/cli.pub:/home/trader/.config/mmr/keys/rpc/cli.pub:ro
      - ${HOME}/.config/mmr/keys/private/signing.pem:/home/trader/.config/mmr/keys/private/signing.pem:ro
      - ${HOME}/.config/mmr/trader.yaml:/home/trader/.config/mmr/trader.yaml:ro
      - ${HOME}/.config/mmr/execution_costs.yaml:/home/trader/.config/mmr/execution_costs.yaml:ro
      - /dev/null:/home/trader/.config/mmr/service_hmac.key:ro
      - mmr_db_data:/home/trader/.local/share/mmr/data
      - mmr_research_data:/home/trader/.local/share/mmr/research
      - ${HOME}/.local/share/mmr/artifacts:/home/trader/.local/share/mmr/artifacts
      - ${HOME}/.local/share/mmr/logs:/home/trader/.local/share/mmr/logs
    healthcheck:
      test: ["CMD", "python3", "-c", "import socket; socket.create_connection((socket.gethostname(), 42106), timeout=3).close()"]
      interval: 30s
      timeout: 5s
      retries: 3
      start_period: 30s
```

Add `mmr_research_data:` under `volumes:`. In `ai` and `cli` add the bind `${HOME}/.config/mmr/keys/rpc/research.pub:/home/trader/.config/mmr/keys/rpc/research.pub:ro` (Plan 1 already added it to `trader`). In `data`, `trader`, `strategy`, `dashboard`, `scheduler`, `cli` add, after their `keys/rpc` tmpfs:

```yaml
      # The research signing key lives only in the research container (SP2c ruling 14).
      - type: tmpfs
        target: /home/trader/.config/mmr/keys/private
        tmpfs:
          size: 4096
          mode: 0700
```

In `trader` keep `- ${HOME}/.config/mmr/keys/verify:/home/trader/.config/mmr/keys/verify:ro` (Plan 2 Task 6 adds it; add it only if it is missing, never twice; a directory bind: a missing directory is just empty).

`docker.sh`:

```bash
# Single-file binds under keys/private (the research signing key). Same trap as the RPC keys.
_compose_private_key_files() {
    grep -oE '\$\{HOME\}/\.config/mmr/keys/private/[a-z_.]+' "$BUILDDIR/docker-compose.yml" \
        | sed -E 's|^.*/||' | sort -u
}
```

At the end of `_require_rpc_keys`, before its closing brace:

```bash
    for name in $(_compose_private_key_files); do
        if [[ ! -f "$HOME/.config/mmr/keys/private/$name" || -L "$HOME/.config/mmr/keys/private/$name" ]]; then
            echo "Error: $HOME/.config/mmr/keys/private/$name is missing or not a regular file."
            echo "Create it once on the host: mmr research attest bundle <artifact> (or restore it from backup)."
            exit 1
        fi
    done
```

`KEYCHECK_SERVICES="trader strategy dashboard cli scheduler data ai research"`, and after the `mmr_ai_data` cleanup line: `$RUNTIME volume rm "${KEYCHECK_PROJECT}_mmr_research_data" >/dev/null 2>&1 || true`.

`keys_cli.py`:

```python
SIGNING_KEY_HOLDERS = frozenset({"research"})
SIGNING_KEY_NAME = "signing.pem"


def signing_key_problems(service: str, private_dir: Path) -> list[str]:
    """The research signing key is visible in its holder only (SP2c spec 5.1)."""
    key = Path(private_dir) / SIGNING_KEY_NAME
    if service in SIGNING_KEY_HOLDERS:
        return [] if key.is_file() else [f"missing {key}"]
    return [f"unexpected {key}"] if key.exists() else []
```

In `_mount_problems`, before the HMAC check: `problems += signing_key_problems(service, keys_dir.parent / "private")`.

- [ ] **Step 4: Run** `.venv/bin/python -m pytest tests/test_compose_research_service.py tests/test_compose_rpc_keys.py tests/test_compose_ai_service.py tests/test_keys_check_mount.py tests/test_docker_helper.py -q` — expected: PASS (the journal test skipped until Task 8).

- [ ] **Step 5: Commit** `feat: add the research container and keep the signing key in it only`

```
feat: add the research container and keep the signing key in it only

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
```

### Task 3: Config, the shared spec builder and request checks

**Files:** Create `trader/research/service_config.py`, `trader/research/cohort.py`, `tests/research/test_cohort_request.py`. Modify `trader/research/evaluation_spec.py`, `config_defaults/trader.yaml`.

**Interfaces.**
- Consumes Plan 1's `EvaluationRequestBody`, `evaluation_request_id`, `split_strategy_key`, `BacktestJudgeConfig` (`allows`, `max_cohort_points`).
- Produces `ResearchServiceConfig` (fields of ruling 3), `load_research_service_config(raw: Mapping) -> ResearchServiceConfig`.
- Produces `build_evaluation_spec(raw: Mapping, *, universe_accessor, costs_config, repo_root: Path) -> EvaluationSpec` and public `declared_tunables(strategy_file: Path, class_name: str) -> set[str]`; `load_evaluation_spec` parses YAML and calls `build_evaluation_spec`.
- Produces in `cohort.py`: `RequestRefused(code, detail)`, `request_body(raw, research_day) -> EvaluationRequestBody`, `neighbours_of(point) -> tuple[dict, ...]`, `evaluation_period(research_day, config) -> (date, date)`, `require_fresh_holdout(windows, holdout_start)`, `previously_revealed(windows, sessions) -> list[str]`, `CohortSpec(request_id, body, strategy_key, base, cohort, neighbours, file_hash)`, `build_cohort_spec(body, *, config, judge, universe_accessor, costs_config, repo_root, registry) -> CohortSpec`.

- [ ] **Step 1: Failing test** `tests/research/test_cohort_request.py`

```python
import datetime as dt

import pytest

from tests.research.evaluation_fixtures import CONIDS, build_spec_file, write_costs_config, write_universe
from trader.automation.backtest_judge_config import BacktestJudgeConfig
from trader.research.cohort import (RequestRefused, build_cohort_spec, neighbours_of, request_body,
                                    require_fresh_holdout)
from trader.research.evaluation_request import evaluation_request_id
from trader.research.service_config import ResearchServiceConfig, load_research_service_config
from trader.simulation.execution_costs import load_execution_costs_config

KEY = "strategies/time_of_day.py:TimeOfDay"
TODAY = dt.date(2024, 3, 29)
CONFIG = ResearchServiceConfig(period_sessions=38, folds=2, embargo_sessions=1, holdout_sessions=5)
JUDGE = BacktestJudgeConfig(strategy_allowlist=(KEY,), max_cohort_points=3)


class Windows:
    def __init__(self, windows=()):
        self.windows = list(windows)

    def opened_holdout_windows(self, path, cls):
        return self.windows


def raw(**overrides):
    return {"strategy_key": KEY, "cohort": [{"ENTRY_MINUTE": 600}], "conids": CONIDS, "bar_size": "15 mins",
            **overrides}


@pytest.fixture
def build(tmp_path, tmp_duckdb_path):
    from trader.data.universe import UniverseAccessor
    write_universe(tmp_duckdb_path)
    build_spec_file(tmp_path)
    costs = load_execution_costs_config(str(write_costs_config(tmp_path / "execution_costs.yaml")))

    def _build(request, windows=(), judge=JUDGE):
        return build_cohort_spec(request_body(request, TODAY), config=CONFIG, judge=judge,
                                 universe_accessor=UniverseAccessor(tmp_duckdb_path, "Universes"),
                                 costs_config=costs, repo_root=tmp_path, registry=Windows(windows))
    return _build


def test_the_service_sets_the_day_so_the_id_changes_per_day():
    today, tomorrow = request_body(raw(), TODAY), request_body(raw(), dt.date(2024, 4, 1))
    assert today.research_day == "2024-03-29"
    assert evaluation_request_id(today) != evaluation_request_id(tomorrow)
    with pytest.raises(RequestRefused) as refused:
        request_body({**raw(), "research_day": "2020-01-01"}, TODAY)     # the caller cannot pick the day
    assert refused.value.code == "REQUEST_INVALID"


def test_neighbours_are_code_derived():
    assert neighbours_of({"A": 600}) == ({"A": 540}, {"A": 660})
    assert neighbours_of({"A": 3, "B": 1.5}) == ({"A": 2, "B": 1.5}, {"A": 4, "B": 1.5},
                                                 {"A": 3, "B": 1.35}, {"A": 3, "B": 1.65})
    assert neighbours_of({"FLAG": True, "NAME": "x"}) == ()


def test_a_good_request_freezes_period_cohort_and_file_hash(build):
    spec = build(raw(cohort=[{"ENTRY_MINUTE": 600}, {"ENTRY_MINUTE": 615}]))
    assert spec.base.period_end == dt.date(2024, 3, 28) and spec.base.holdout_sessions == 5
    assert [len(n) for n in spec.neighbours] == [2, 2] and spec.file_hash.startswith("sha256:")
    assert spec.request_id == evaluation_request_id(spec.body)


@pytest.mark.parametrize("request_raw,code", [
    (raw(strategy_key="strategies/other.py:Other"), "STRATEGY_NOT_ALLOWED"),
    (raw(cohort=[{"ENTRY_MINUTE": m} for m in (600, 615, 630, 645)]), "COHORT_TOO_LARGE"),
    (raw(cohort=[{"ENTRY_MINUTE": 600}, {"ENTRY_MINUTE": 600}]), "REQUEST_INVALID"),
    (raw(conids=list(reversed(CONIDS))), "REQUEST_INVALID"),
    (raw(cohort=[{"UNKNOWN": 1}]), "SPEC_INVALID"),
    (raw(cohort=[{"ENTRY_MINUTE": 600}, {"NOT_A_TUNABLE": 1}]), "COHORT_POINT_INVALID"),
    (raw(cohort=[{"NAME": "x"}]), "COHORT_POINT_INVALID"),
    (raw(conids=CONIDS[:7]), "CONIDS_OUT_OF_SCOPE"),
    (raw(bar_size="1 hour"), "REQUEST_INVALID"),
    ({**raw(), "extra": 1}, "REQUEST_INVALID"),
])
def test_bad_requests_are_refused_by_code(build, request_raw, code):
    with pytest.raises(RequestRefused) as refused:
        build(request_raw)
    assert refused.value.code == code


def test_a_revealed_window_blocks_any_holdout_that_does_not_start_after_it(build):
    revealed = {"artifact_id": "a", "family_id": "f", "start": dt.date(2024, 3, 20), "end": dt.date(2024, 3, 27)}
    with pytest.raises(RequestRefused) as refused:
        build(raw(), windows=[revealed])                              # the new holdout ends 2024-03-28
    assert refused.value.code == "HOLDOUT_NOT_AVAILABLE"
    require_fresh_holdout([revealed], dt.date(2024, 3, 28))          # starts after: allowed
    with pytest.raises(RequestRefused):
        require_fresh_holdout([revealed], dt.date(2024, 3, 27))      # a shifted window still touches it


def test_config_block_refuses_unknown_keys_and_bad_values():
    assert load_research_service_config({}) == ResearchServiceConfig()
    with pytest.raises(ValueError):
        load_research_service_config({"research_service": {"folds": 0}})
    with pytest.raises(ValueError):
        load_research_service_config({"research_service": {"surprise": 1}})
```

- [ ] **Step 2: Run** `.venv/bin/python -m pytest tests/research/test_cohort_request.py -q` — expected: FAIL (`ModuleNotFoundError: trader.research.cohort`).

- [ ] **Step 3: Implement.**

`evaluation_spec.py`: rename `_tunables` to `declared_tunables` (update its one caller); move everything in `load_evaluation_spec` after the YAML parse into `build_evaluation_spec(raw, *, universe_accessor, costs_config, repo_root)`; `load_evaluation_spec` becomes:

```python
def load_evaluation_spec(path: str | Path, *, universe_accessor: Any,
                         costs_config: ExecutionCostsConfig, repo_root: Path) -> EvaluationSpec:
    try:
        loaded = yaml.safe_load(Path(path).read_text())
    except yaml.YAMLError as exc:
        raise EvaluationSpecError(f'spec: not valid YAML: {exc}') from exc
    return build_evaluation_spec(_mapping(loaded or {}, 'spec'), universe_accessor=universe_accessor,
                                 costs_config=costs_config, repo_root=repo_root)
```

`service_config.py`:

```python
"""The research service's evaluation defaults (`research_service:` in trader.yaml; SP2c Plan 3 ruling 3)."""
from __future__ import annotations

import math
from dataclasses import dataclass, fields
from typing import Any, Mapping

_INTEGER_FIELDS = ("period_sessions", "folds", "embargo_sessions", "holdout_sessions", "queue_max",
                   "shadow_incomplete_after_hours")


class ResearchConfigError(ValueError):
    pass


@dataclass(frozen=True)
class ResearchServiceConfig:
    period_sessions: int = 690
    folds: int = 6
    embargo_sessions: int = 5
    holdout_sessions: int = 90
    order_notional: float = 1900.0
    account_equity: float = 100_000.0
    max_gross_allocation: float = 0.05
    queue_max: int = 20
    shadow_incomplete_after_hours: int = 16


def _value(name: str, value: Any) -> Any:
    integer = name in _INTEGER_FIELDS
    if isinstance(value, bool) or not isinstance(value, int if integer else (int, float)):
        raise ResearchConfigError(f"research_service.{name}: must be {'an integer' if integer else 'a number'}")
    if not math.isfinite(value) or value < 0 or (value == 0 and name != "embargo_sessions"):
        raise ResearchConfigError(f"research_service.{name}: must be positive, got {value!r}")
    return value if integer else float(value)


def load_research_service_config(raw: Mapping[str, Any]) -> ResearchServiceConfig:
    block = raw.get("research_service") or {}
    if not isinstance(block, Mapping):
        raise ResearchConfigError("research_service: must be a mapping")
    unknown = sorted(set(block) - {f.name for f in fields(ResearchServiceConfig)})
    if unknown:
        raise ResearchConfigError(f"research_service: unknown keys {unknown}")
    config = ResearchServiceConfig(**{name: _value(name, value) for name, value in block.items()})
    if config.max_gross_allocation > 1:
        raise ResearchConfigError("research_service.max_gross_allocation: must be in (0, 1]")
    if config.holdout_sessions + 2 * config.folds >= config.period_sessions:
        raise ResearchConfigError("research_service.period_sessions: too short for the folds and the holdout")
    return config
```

`cohort.py`:

```python
"""Checks of one `submit_evaluation` request, by code, before any claim (SP2c spec 5.1 step 2, 6.2)."""
from __future__ import annotations

import datetime as dt
import hashlib
import math
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

import exchange_calendars as xcals
import pandas as pd
from pydantic import ValidationError

from trader.research.evaluation_request import EvaluationRequestBody, evaluation_request_id
from trader.research.evaluation_spec import (MIN_INSTRUMENTS, EvaluationSpec, EvaluationSpecError,
                                             build_evaluation_spec, declared_tunables)
from trader.research.strategy_key import split_strategy_key
from trader.research.validation import generate_walk_forward

NEIGHBOUR_SHARE = 0.1


class RequestRefused(Exception):
    def __init__(self, code: str, detail: str):
        super().__init__(f"{code}: {detail}")
        self.code, self.detail = code, detail


def request_body(raw: Mapping[str, Any], research_day: dt.date) -> EvaluationRequestBody:
    """Plan 1's canonical claim body: the caller's four fields plus the service's New York day (ruling 1)."""
    if not isinstance(raw, Mapping) or "research_day" in raw:
        raise RequestRefused("REQUEST_INVALID", "the research service sets research_day")
    try:
        return EvaluationRequestBody.model_validate({**raw, "research_day": research_day.isoformat()})
    except ValidationError as exc:
        raise RequestRefused("REQUEST_INVALID", exc.errors()[0]["msg"]) from None


def _neighbour_values(value: Any) -> tuple:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return ()
    if isinstance(value, int):
        step = max(1, round(abs(value) * NEIGHBOUR_SHARE))
        return (value - step, value + step)
    if not math.isfinite(value) or value == 0:
        return ()
    return tuple(float(f"{v:.6g}") for v in (value * (1 - NEIGHBOUR_SHARE), value * (1 + NEIGHBOUR_SHARE)))


def neighbours_of(point: Mapping[str, Any]) -> tuple[dict, ...]:
    """Ruling 2: one key changes at a time, ±10 %."""
    return tuple({**point, key: v} for key, value in point.items() for v in _neighbour_values(value) if v != value)


def evaluation_period(research_day: dt.date, config: Any) -> tuple[dt.date, dt.date]:
    """The last complete XNYS session before ``research_day`` closes the period."""
    calendar = xcals.get_calendar("XNYS")
    end = calendar.date_to_session(pd.Timestamp(research_day), direction="previous")
    if end.date() >= research_day:
        end = calendar.previous_session(end)
    start = calendar.session_offset(end, -(config.period_sessions - 1))
    return start.date(), end.date()


def require_fresh_holdout(windows: Sequence[Mapping[str, Any]], holdout_start: dt.date) -> None:
    """Spec 6.2: a new holdout lies after every revealed session; shifting by a day does not help."""
    if not windows:
        return
    last_revealed = max(w["end"] for w in windows)
    if holdout_start <= last_revealed:
        raise RequestRefused("HOLDOUT_NOT_AVAILABLE",
                             f"the next holdout must start after {last_revealed}; it would start {holdout_start}")


def previously_revealed(windows: Sequence[Mapping[str, Any]], sessions: Sequence[dt.date]) -> list[str]:
    return [d.isoformat() for d in sessions if any(w["start"] <= d <= w["end"] for w in windows)]


@dataclass(frozen=True)
class CohortSpec:
    request_id: str
    body: EvaluationRequestBody               # the exact claim body
    strategy_key: str
    base: EvaluationSpec                       # params = cohort[0]; carries period, folds, costs, conids
    cohort: tuple[Mapping[str, Any], ...]
    neighbours: tuple[tuple[dict, ...], ...]   # per cohort point; never selectable
    file_hash: str


def _file_hash(path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def build_cohort_spec(body: EvaluationRequestBody, *, config: Any, judge: Any, universe_accessor: Any,
                      costs_config: Any, repo_root, registry: Any) -> CohortSpec:
    if not judge.allows(body.strategy_key):
        raise RequestRefused("STRATEGY_NOT_ALLOWED", f"{body.strategy_key} is not on the allowlist")
    cohort = [dict(point) for point in body.cohort]
    if len(cohort) > judge.max_cohort_points:
        raise RequestRefused("COHORT_TOO_LARGE", f"a cohort has at most {judge.max_cohort_points} points")
    if len(body.conids) < MIN_INSTRUMENTS:
        raise RequestRefused("CONIDS_OUT_OF_SCOPE", f"at least {MIN_INSTRUMENTS} distinct conids required")
    neighbours = tuple(neighbours_of(point) for point in cohort)
    if not all(neighbours):
        raise RequestRefused("COHORT_POINT_INVALID", "every point needs a numeric tunable for its neighbours")
    path, class_name = split_strategy_key(body.strategy_key)
    start, end = evaluation_period(dt.date.fromisoformat(body.research_day), config)
    first = cohort[0]
    raw = {"name": f"ai-{evaluation_request_id(body)[7:19]}", "strategy": path, "class": class_name,
           "params": first,
           "neighbourhood": {key: [n[key] for n in neighbours[0] if n[key] != first[key]]
                             for key in first if any(n[key] != first[key] for n in neighbours[0])},
           "conids": list(body.conids), "bar_size": body.bar_size, "period": {"start": start, "end": end},
           "walk_forward": {"folds": config.folds, "embargo_sessions": config.embargo_sessions,
                            "holdout_sessions": config.holdout_sessions},
           "sizing": {"order_notional": config.order_notional, "account_equity": config.account_equity},
           "max_gross_allocation": config.max_gross_allocation}
    try:
        base = build_evaluation_spec(raw, universe_accessor=universe_accessor, costs_config=costs_config,
                                     repo_root=repo_root)
    except EvaluationSpecError as exc:
        raise RequestRefused("SPEC_INVALID", str(exc)) from None
    tunables = declared_tunables(base.strategy_file, base.class_name)
    for index, point in enumerate(cohort[1:], start=1):
        if set(point) - tunables:
            raise RequestRefused("COHORT_POINT_INVALID", f"point {index}: not tunables {sorted(set(point) - tunables)}")
    plan = generate_walk_forward((base.period_start, base.period_end), n_folds=base.folds,
                                 embargo=base.embargo_sessions, holdout=base.holdout_sessions,
                                 calendar_name=base.calendar)
    require_fresh_holdout(registry.opened_holdout_windows(base.strategy_path, base.class_name),
                          pd.Timestamp(plan.holdout.start).date())
    return CohortSpec(request_id=evaluation_request_id(body), body=body, strategy_key=body.strategy_key,
                      base=base, cohort=tuple(cohort), neighbours=neighbours, file_hash=_file_hash(base.strategy_file))
```

`config_defaults/trader.yaml` — after the `ai_paper:` block:

```yaml
# research service (SP2c): evaluation defaults for AI-proposed candidates. Operator only.
research_service:
  period_sessions: 690        # sessions before the research day; the last holdout_sessions are the holdout
  folds: 6
  embargo_sessions: 5
  holdout_sessions: 90
  order_notional: 1900
  account_equity: 100000
  max_gross_allocation: 0.05
  queue_max: 20               # a full queue refuses before any claim
  shadow_incomplete_after_hours: 16
```

- [ ] **Step 4: Run** `.venv/bin/python -m pytest tests/research/test_cohort_request.py tests/research/test_evaluation_spec.py tests/test_research_evaluate_cli.py -q` — expected: PASS.

- [ ] **Step 5: Commit** `feat: check research evaluation requests by code before any claim`

```
feat: check research evaluation requests by code before any claim

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
```

### Task 4: Cohort evaluation (pre-holdout per point, code selection, one holdout)

**Files:** Modify `trader/research/evaluation.py` (extract two helpers, no behaviour change). Create `trader/research/cohort_evaluation.py`, `tests/research/test_cohort_evaluation.py`.

**Interfaces.**
- Produces in `evaluation.py`: `PointContext(sessions, regimes, liquidity, missing_causes, market_context)`, `point_market_context(spec, plan, point, benchmark_closes, bars) -> PointContext`, `HoldoutOutcome(artifact_id, passed, evidence, decision, decision_digest, window: dict)`, `run_holdout(research_db, registry, spec, env, plan, family_id, point, evidence, benchmark_closes, context, now, max_workers, ruleset) -> HoldoutOutcome`, `run_environment(spec, paths) -> RunEnvironment`. `evaluate()` calls them.
- Produces in `cohort_evaluation.py`: `PointVerdict`, `select_point(verdicts) -> Optional[PointVerdict]`, `replay_index(verdicts, selected) -> int`, `CohortResult`, `evaluate_cohort(spec: CohortSpec, *, research_db, paths, now, ruleset=PAPER_V1, max_workers=1) -> CohortResult`.

- [ ] **Step 1: Refactor `evaluation.py` (behaviour unchanged).** Move the regime/liquidity block of `evaluate` into:

```python
@dataclass(frozen=True)
class PointContext:
    sessions: list
    regimes: Any
    liquidity: Any
    missing_causes: dict
    market_context: dict


def point_market_context(spec, plan, point: PointResult, benchmark_closes, bars) -> PointContext:
    holdout_start = pd.Timestamp(plan.holdout.start).date()
    try:
        sessions = _period_session_dates(spec)
        labels = mcx.regime_labels(benchmark_closes, sessions)
        walk_trips = mcx.annotate_regimes(_round_trips(point.outcomes[1.0]), labels)
        regimes = mcx.regime_evidence(walk_trips, labels.loc[[d for d in sessions if d < holdout_start]])
        liquidity = mcx.liquidity_envelope(bars, order_notional=spec.order_notional, before=holdout_start)
    except mcx.MarketContextError as exc:
        raise EvaluationError(f'market context unavailable: {exc}') from exc
    missing = _missing_causes({
        'liquidity_capacity_envelope': liquidity.within,
        'regime_positive_expectancy_fraction': regimes.positive_fraction,
        'regime_loss_tolerance': regimes.worst_loss,
        'regime_transition_stability': regimes.transitions_stable})
    return PointContext(sessions, regimes, liquidity, missing,
                        {'regimes': _regime_context(regimes), 'liquidity': _liquidity_context(liquidity)})
```

Move everything from `seal_artifact` to `decision_digest = ...record(...)` into `run_holdout` (same statements; `selected_trial_id=point.trial_id`, `selected_parameters=dict(point.params)`, the window job params `dict(point.params)`; it updates `context.missing_causes` and `context.market_context['benchmark']` as today) returning `HoldoutOutcome(artifact_id, passed, evidence, decision, decision_digest if passed else None, {'start': holdout_start.isoformat(), 'end': holdout_end.isoformat(), 'passed': passed, 'detail': detail})`; it records the decision only when `passed`, exactly as today. Move the `RunEnvironment(...)` construction into `run_environment(spec, paths)`. `evaluate` then reads:

```python
    env = run_environment(spec, paths)
    main = _run_point(registry, family_id, env, plan, dict(spec.params), COST_MULTIPLIERS,
                      now, max_workers, rerun_existing=True)
    neighbours = [_run_point(registry, family_id, env, plan, point, (1.0,), now, max_workers,
                             rerun_existing=False) for point in neighbour_points(spec)]
    context = point_market_context(spec, plan, main, benchmark_closes, bars)
    evidence = _walk_forward_evidence(
        spec, main, neighbours, registry.strategy_trials(spec.strategy_path, spec.class_name),
        context.regimes, context.liquidity)
    gate = ev.pre_holdout_outcome(evaluate_eligibility(ruleset, evidence))
    finish = dict(family_id=family_id, main=main, neighbours=neighbours,
                  market_context=context.market_context, missing_causes=context.missing_causes)
    if not gate.passed:
        return _finish(research_db, spec, paths, now, stage=STAGE_PRE_HOLDOUT, state='CANDIDATE',
                       artifact_id=None, decision_digest=None, gate=gate, evidence=evidence, **finish)
    outcome = run_holdout(research_db, registry, spec, env, plan, family_id, main, evidence,
                          benchmark_closes, context, now, max_workers, ruleset)
    if not outcome.passed:
        # paper-v1 has no holdout-expectancy rule, so the decision alone could still
        # read PAPER_ELIGIBLE. Record none: a retired artifact must never be attested.
        return _finish(research_db, spec, paths, now, stage=STAGE_HOLDOUT_FAILED,
                       state=ARTIFACT_STATE_RETIRED, artifact_id=outcome.artifact_id, decision_digest=None,
                       gate=ev.final_outcome(outcome.decision), evidence=outcome.evidence, **finish)
    return _finish(research_db, spec, paths, now, stage=STAGE_COMPLETE, state=outcome.decision.state,
                   artifact_id=outcome.artifact_id, decision_digest=outcome.decision_digest,
                   gate=ev.final_outcome(outcome.decision), evidence=outcome.evidence, **finish)
```

Run `.venv/bin/python -m pytest tests/research tests/test_research_evaluate_cli.py -q` — expected: PASS (the refactor changes nothing). Commit `refactor: split the evaluator's holdout and market context into helpers`.

- [ ] **Step 2: Failing test** `tests/research/test_cohort_evaluation.py`

```python
import datetime as dt
from dataclasses import replace
from types import SimpleNamespace

import pytest

from tests.research.evaluation_fixtures import (CONIDS, build_spec_file, write_costs_config, write_trend_bars,
                                                write_universe)
from trader.data.duckdb_store import DuckDBConnection
from trader.data.schema_migrations import SchemaMigrator
from trader.data.universe import UniverseAccessor
from trader.research import cohort_evaluation as ce
from trader.research import evaluation
from trader.automation.backtest_judge_config import BacktestJudgeConfig
from trader.research.cohort import build_cohort_spec, request_body
from trader.research.evaluation import EvaluationPaths, HoldoutOutcome
from trader.research.evaluation_jobs import WindowOutcome
from trader.research.evidence import GateOutcome
from trader.research.experiment_registry import ExperimentRegistry
from trader.research.schema import apply_research_migrations
from trader.research.service_config import ResearchServiceConfig
from trader.simulation.execution_costs import load_execution_costs_config

KEY = "strategies/time_of_day.py:TimeOfDay"
TODAY = dt.date(2024, 3, 29)
NOW = dt.datetime(2024, 3, 29, 21, tzinfo=dt.timezone.utc)


@pytest.fixture
def world(tmp_path, tmp_duckdb_path, monkeypatch):
    write_universe(tmp_duckdb_path)
    write_trend_bars(tmp_duckdb_path, drift=0.0006)
    build_spec_file(tmp_path)
    costs = write_costs_config(tmp_path / "execution_costs.yaml")
    db = DuckDBConnection.get_instance(str(tmp_path / "research.duckdb"))
    apply_research_migrations(SchemaMigrator(db))
    paths = EvaluationPaths(tmp_duckdb_path, tmp_duckdb_path, "Universes", str(costs), tmp_path,
                            tmp_path / "reports", tmp_path / "artifacts" / "evaluations")
    jobs, gates, opened = [], {}, []

    def fake_run_jobs(env, batch, max_workers=1):
        jobs.extend(batch)
        return [WindowOutcome(job, (), ((job.start, 100_000.0),), 0.0, 0.0, "flat", {}) for job in batch]

    def fake_verdict(index, base, plan, point, neighbours, trials, closes, bars, ruleset):
        passed, statistic = gates[index]
        return ce.PointVerdict(index, point, tuple(neighbours), GateOutcome(passed, () if passed else ("x",), ()),
                               None, SimpleNamespace(selection_adjusted_confidence=statistic), None)

    def fake_holdout(research_db, registry, spec, env, plan, family_id, point, evidence, *rest):
        aid = registry.seal_artifact(family_id, selected_trial_id=point.trial_id,
                                     selected_parameters=dict(point.params), sealed_at=NOW)
        registry.open_holdout(aid, opened_at=NOW, passed=True)
        opened.append(dict(point.params))
        return HoldoutOutcome(aid, True, evidence, SimpleNamespace(state="PAPER_ELIGIBLE"), "d" * 64,
                              {"start": "2024-03-22", "end": "2024-03-28", "passed": True, "detail": ""})

    monkeypatch.setattr(evaluation, "run_jobs", fake_run_jobs)
    monkeypatch.setattr(ce, "_verdict", fake_verdict)
    monkeypatch.setattr(ce, "run_holdout", fake_holdout)
    monkeypatch.setattr(ce, "_record_evaluation", lambda *args, **kwargs: None)
    config = ResearchServiceConfig(period_sessions=38, folds=2, embargo_sessions=1, holdout_sessions=5)
    judge = BacktestJudgeConfig(strategy_allowlist=(KEY,), max_cohort_points=3)

    def run(cohort, statistics):
        gates.update(statistics)
        spec = build_cohort_spec(
            request_body({"strategy_key": KEY, "cohort": cohort, "conids": CONIDS, "bar_size": "15 mins"}, TODAY),
            config=config, judge=judge, universe_accessor=UniverseAccessor(tmp_duckdb_path, "Universes"),
            costs_config=load_execution_costs_config(str(costs)), repo_root=tmp_path,
            registry=ExperimentRegistry(db))
        return spec, ce.evaluate_cohort(spec, research_db=db, paths=paths, now=lambda: NOW)
    return SimpleNamespace(run=run, jobs=jobs, opened=opened, registry=ExperimentRegistry(db), db=db, paths=paths)


def test_the_best_point_that_fails_the_gate_is_not_selected(world):
    spec, result = world.run([{"ENTRY_MINUTE": 600}, {"ENTRY_MINUTE": 615}], {0: (False, 0.99), 1: (True, 0.6)})
    assert result.selected.index == 1 and world.opened == [{"ENTRY_MINUTE": 615}]
    assert result.stage == "complete" and len(world.registry.family_artifacts(result.family_id)) == 1


def test_every_cohort_point_runs_full_cost_stress_and_neighbours_only_at_1x(world):
    spec, result = world.run([{"ENTRY_MINUTE": 600}, {"ENTRY_MINUTE": 615}], {0: (True, 0.5), 1: (True, 0.5)})
    by_point = {}
    for job in world.jobs:
        by_point.setdefault(job.point_key, set()).add(job.cost_multiplier)
    cohort_keys = {evaluation._point_key(p) for p in spec.cohort}
    assert all(by_point[k] == {1.0, 1.5, 2.0} for k in cohort_keys)
    assert all(v == {1.0} for k, v in by_point.items() if k not in cohort_keys and k.startswith("point:"))
    assert result.selected.index == 0                                   # tie: cohort order
    assert result.selected.point.params in spec.cohort                 # a neighbour is never selectable


def test_no_passing_point_seals_nothing_and_opens_no_holdout(world):
    spec, result = world.run([{"ENTRY_MINUTE": 600}], {0: (False, 0.9)})
    assert result.stage == "pre_holdout" and result.selected is None and result.holdout is None
    assert world.registry.family_artifacts(result.family_id) == [] and world.opened == []
    assert result.replay_index == 0


def test_one_point_and_two_neighbours_add_three_trials(world):
    spec, result = world.run([{"ENTRY_MINUTE": 600}], {0: (False, 0.1)})
    assert len(world.registry.strategy_trials("strategies/time_of_day.py", "TimeOfDay")) == 3
    assert result.strategy_trials == 3


def test_a_neighbour_equal_to_another_cohort_point_is_one_trial(world):
    spec, result = world.run([{"ENTRY_MINUTE": 600}, {"ENTRY_MINUTE": 660}], {0: (False, 0.1), 1: (False, 0.2)})
    assert len(world.registry.strategy_trials("strategies/time_of_day.py", "TimeOfDay")) == 5   # not 6


def test_previously_revealed_sessions_are_labelled(world):
    registry = world.registry
    spec, first = world.run([{"ENTRY_MINUTE": 600}], {0: (False, 0.1)})
    family = replace(registry.get_family(first.family_id), search_space={"earlier": True})
    fid = registry.create_family(family, created_at=NOW, validation_folds=[
        {"kind": "holdout", "start": "2024-02-20", "end": "2024-02-23"}])
    tid = registry.start_trial(fid, trial_key="point:{}#1", parameters={}, started_at=NOW)
    registry.finish_trial(tid, status="SUCCEEDED", finished_at=NOW, metrics={"daily_sharpe": 0.1})
    registry.open_holdout(registry.seal_artifact(fid, selected_trial_id=tid, selected_parameters={},
                                                 sealed_at=NOW), opened_at=NOW, passed=False)
    _, later = world.run([{"ENTRY_MINUTE": 615}], {0: (False, 0.1)})
    assert later.previously_revealed == ["2024-02-20", "2024-02-21", "2024-02-22", "2024-02-23"]
    assert later.holdouts_opened_before == 1
```

- [ ] **Step 3: Run** `.venv/bin/python -m pytest tests/research/test_cohort_evaluation.py -q` — expected: FAIL (`ModuleNotFoundError: trader.research.cohort_evaluation`).

- [ ] **Step 4: Implement** `trader/research/cohort_evaluation.py`

```python
"""Cohort evaluation (SP2c spec 5.1, 6.2): every point through the full pre-holdout gate,
a code selection on walk-forward evidence only, then one holdout for the selected point."""
from __future__ import annotations

import datetime as dt
import math
from dataclasses import dataclass, replace
from typing import Any, Callable, Optional, Sequence

import pandas as pd

from trader.research import evidence as ev
from trader.research.artifact import ARTIFACT_STATE_RETIRED
from trader.research.cohort import CohortSpec, previously_revealed, require_fresh_holdout
from trader.research.eligibility import evaluate_eligibility
from trader.research.evaluation import (
    COST_MULTIPLIERS, EvaluationPaths, HoldoutOutcome, PointResult, _day_end, _day_start, _family, _finish,
    _fold_specs, _period_session_dates, _point_key, _refuse_if_holdout_opened, _repository_commit, _run_point,
    _walk_forward_evidence, point_market_context, run_environment, run_holdout)
from trader.research.evaluation_data import load_bars, load_benchmark_closes, qualify_dataset
from trader.research.evaluation_store import STAGE_COMPLETE, STAGE_HOLDOUT_FAILED, STAGE_PRE_HOLDOUT
from trader.research.experiment_registry import ExperimentRegistry
from trader.research.rulesets.paper_v1 import PAPER_V1
from trader.research.schema import DatasetManifestRepository
from trader.research.validation import generate_walk_forward


@dataclass(frozen=True)
class PointVerdict:
    index: int
    point: PointResult
    neighbours: tuple[PointResult, ...]
    gate: ev.GateOutcome
    decision: Any            # the pre-holdout EligibilityDecision (every rule result)
    evidence: Any            # EligibilityEvidence
    context: Any             # PointContext


@dataclass(frozen=True)
class CohortResult:
    family_id: str
    stage: str
    verdicts: tuple[PointVerdict, ...]
    strategy_trials: int
    selected: Optional[PointVerdict]
    replay_index: int
    holdout: Optional[HoldoutOutcome]
    previously_revealed: list[str]
    holdouts_opened_before: int


def _statistic(verdict: PointVerdict) -> float:
    value = verdict.evidence.selection_adjusted_confidence
    return value if isinstance(value, (int, float)) and math.isfinite(value) else -math.inf


def select_point(verdicts: Sequence[PointVerdict]) -> Optional[PointVerdict]:
    """Ruling 5: highest deflated statistic among points that pass the full pre-holdout gate; ties by
    cohort order. Holdout data never feeds this choice (no holdout is open yet)."""
    passing = [v for v in verdicts if v.gate.passed]
    return max(passing, key=lambda v: (_statistic(v), -v.index)) if passing else None


def replay_index(verdicts: Sequence[PointVerdict], selected: Optional[PointVerdict]) -> int:
    """Ruling 7: the point the shadow replay runs."""
    if selected is not None:
        return selected.index
    scored = [v for v in verdicts if _statistic(v) > -math.inf]
    return max(scored, key=lambda v: (_statistic(v), -v.index)).index if scored else 0


def _verdict(index, base, plan, point, neighbours, trials, benchmark_closes, bars, ruleset) -> PointVerdict:
    context = point_market_context(base, plan, point, benchmark_closes, bars)
    evidence = _walk_forward_evidence(base, point, neighbours, trials, context.regimes, context.liquidity)
    decision = evaluate_eligibility(ruleset, evidence)
    return PointVerdict(index, point, tuple(neighbours), ev.pre_holdout_outcome(decision), decision, evidence,
                        context)


def _cohort_search_space(spec: CohortSpec) -> dict:
    return {"cohort": [dict(p) for p in spec.cohort],
            "neighbourhood": {_point_key(p): [dict(n) for n in ns] for p, ns in zip(spec.cohort, spec.neighbours)}}


def _pre_holdout_sessions(base, plan) -> list[dt.date]:
    holdout_start = pd.Timestamp(plan.holdout.start).date()
    return [d for d in _period_session_dates(base) if d < holdout_start]


def _record_evaluation(research_db, base, paths, now, result: CohortResult, verdict: PointVerdict) -> None:
    """The existing evaluation log and report, for the selected (or replayed) point."""
    outcome = result.holdout
    gate = verdict.gate if outcome is None else ev.final_outcome(outcome.decision)
    state = {STAGE_PRE_HOLDOUT: "CANDIDATE", STAGE_HOLDOUT_FAILED: ARTIFACT_STATE_RETIRED}.get(
        result.stage, None if outcome is None else outcome.decision.state)
    _finish(research_db, replace(base, params=dict(verdict.point.params)), paths, now,
            family_id=result.family_id, stage=result.stage, state=state,
            artifact_id=None if outcome is None else outcome.artifact_id,
            decision_digest=None if outcome is None else outcome.decision_digest, gate=gate,
            evidence=verdict.evidence if outcome is None else outcome.evidence, main=verdict.point,
            neighbours=list(verdict.neighbours), market_context=verdict.context.market_context,
            missing_causes=verdict.context.missing_causes)


def evaluate_cohort(spec: CohortSpec, *, research_db: Any, paths: EvaluationPaths,
                    now: Callable[[], dt.datetime], ruleset=PAPER_V1, max_workers: int = 1) -> CohortResult:
    base = spec.base
    registry = ExperimentRegistry(research_db)
    plan = generate_walk_forward((base.period_start, base.period_end), n_folds=base.folds,
                                 embargo=base.embargo_sessions, holdout=base.holdout_sessions,
                                 calendar_name=base.calendar)
    holdout_start = pd.Timestamp(plan.holdout.start).date()
    windows = registry.opened_holdout_windows(base.strategy_path, base.class_name)
    require_fresh_holdout(windows, holdout_start)
    repository_commit = _repository_commit(paths.repo_root, base.strategy_file)
    bars = load_bars(paths.history_db, base.conids, base.bar_size, _day_start(base.period_start),
                     _day_end(base.period_end))
    benchmark_closes = load_benchmark_closes(paths.history_db, base)
    manifest_digest = DatasetManifestRepository(research_db).seal(
        qualify_dataset(bars, base, benchmark_closes=benchmark_closes), sealed_at=now())
    family = replace(_family(base, manifest_digest, repository_commit, paths),
                     search_space=_cohort_search_space(spec))
    family_id = registry.create_family(family, created_at=now(), validation_folds=_fold_specs(plan))
    _refuse_if_holdout_opened(registry, family_id)
    env = run_environment(base, paths)
    # Cohort points first, so a neighbour equal to a cohort point reuses that trial.
    points = [_run_point(registry, family_id, env, plan, dict(p), COST_MULTIPLIERS, now, max_workers,
                         rerun_existing=True) for p in spec.cohort]
    neighbours = [[_run_point(registry, family_id, env, plan, dict(n), (1.0,), now, max_workers,
                              rerun_existing=False) for n in ns] for ns in spec.neighbours]
    trials = registry.strategy_trials(base.strategy_path, base.class_name)   # one denominator for all points
    verdicts = tuple(_verdict(i, base, plan, p, ns, trials, benchmark_closes, bars, ruleset)
                     for i, (p, ns) in enumerate(zip(points, neighbours)))
    selected = select_point(verdicts)
    common = dict(family_id=family_id, verdicts=verdicts, strategy_trials=len(trials),
                  replay_index=replay_index(verdicts, selected),
                  previously_revealed=previously_revealed(windows, _pre_holdout_sessions(base, plan)),
                  holdouts_opened_before=len(windows))
    if selected is None:
        result = CohortResult(stage=STAGE_PRE_HOLDOUT, selected=None, holdout=None, **common)
    else:
        require_fresh_holdout(registry.opened_holdout_windows(base.strategy_path, base.class_name), holdout_start)
        outcome = run_holdout(research_db, registry, replace(base, params=dict(selected.point.params)), env, plan,
                              family_id, selected.point, selected.evidence, benchmark_closes, selected.context,
                              now, max_workers, ruleset)
        stage = STAGE_COMPLETE if outcome.passed else STAGE_HOLDOUT_FAILED
        result = CohortResult(stage=stage, selected=selected, holdout=outcome, **common)
    _record_evaluation(research_db, base, paths, now, result, selected or verdicts[result.replay_index])
    return result
```

- [ ] **Step 5: Run** `.venv/bin/python -m pytest tests/research/test_cohort_evaluation.py tests/research -q` — expected: PASS.

- [ ] **Step 6: Commit** `feat: evaluate a frozen cohort and open one holdout for the code-selected point`

```
feat: evaluate a frozen cohort and open one holdout for the code-selected point

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
```

### Task 5: Building Plan 1's evaluation case from a result (never authorizing)

**Files:** Create `trader/research/case_builder.py`, `tests/research/case_fixtures.py`, `tests/research/test_case_builder.py`.

**Interfaces.**
- Consumes Plan 1's `EvaluationCase`, `CASE_DOMAIN`, `write_evaluation_case`, `load_verified_case`, `initial_deploy_allowed`, `offered_menu`, `FULL_MENU`, `NO_DEPLOY_MENU`; `split_strategy_key`.
- Produces `STAGE_NAMES`, `json_safe(value)`, `holdout_evidence(outcome) -> dict | None`, `initial_evidence(result, *, warmup_sessions) -> dict`, `build_initial_case(spec, claim_day, result, *, created_at, warmup_sessions) -> EvaluationCase`, `build_failed_case(body, *, request_id, claim_day, file_hash, error, created_at, warmup_sessions) -> EvaluationCase`, `evaluation_summary(case, *, order_notional) -> dict` (Plan 4's `EvaluationSummary` keys).

- [ ] **Step 1: Fixture** `tests/research/case_fixtures.py` (Tasks 5, 6, 7, 11):

```python
"""Small stand-ins for a cohort result and an evaluation case, for the research service tests."""
from types import SimpleNamespace

from trader.research.cohort_evaluation import CohortResult, PointVerdict
from trader.research.evaluation import HoldoutOutcome, PointResult
from trader.research.evidence import GateOutcome
from trader.research.rulesets.paper_v1 import PAPER_V1


def rule_results(passed=True, observed=12.5):
    return tuple(SimpleNamespace(code=rule.code, passed=passed, observed=observed) for rule in PAPER_V1.rules)


def verdict(index, params, trial_id, *, passed, statistic, observed=12.5):
    evidence = SimpleNamespace(expectancy_bps_baseline=12.5, expectancy_bps_1_5x=9.0, expectancy_bps_2x=5.5,
                               selection_adjusted_confidence=statistic)
    decision = SimpleNamespace(results=rule_results(passed, observed), digest="e" * 64, state="CANDIDATE",
                               ruleset_digest=PAPER_V1.digest)
    return PointVerdict(index, PointResult(dict(params), trial_id, {}), (PointResult({}, f"{trial_id}-n", {}),),
                        GateOutcome(passed, () if passed else ("expectancy_2x_positive",), ()), decision, evidence,
                        None)


def pre_holdout_result(params=({"ENTRY_MINUTE": 600},)):
    verdicts = tuple(verdict(i, p, f"t{i}", passed=False, statistic=0.4, observed=float("nan"))
                     for i, p in enumerate(params))
    return CohortResult("f" * 64, "pre_holdout", verdicts, 3, None, 0, None, [], 0)


def complete_result(params=({"ENTRY_MINUTE": 600},), *, holdout_passed=True, artifact_id="a" * 64):
    verdicts = tuple(verdict(i, p, f"t{i}", passed=True, statistic=0.9) for i, p in enumerate(params))
    decision = SimpleNamespace(results=rule_results(True), digest="d" * 64, state="PAPER_ELIGIBLE",
                               ruleset_digest=PAPER_V1.digest)
    outcome = HoldoutOutcome(artifact_id, holdout_passed, verdicts[0].evidence, decision,
                             "d" * 64 if holdout_passed else None,
                             {"start": "2024-03-22", "end": "2024-03-28", "passed": holdout_passed, "detail": "x"})
    stage = "complete" if holdout_passed else "holdout_failed"
    return CohortResult("f" * 64, stage, verdicts, 3, verdicts[0], 0, outcome, ["2024-02-20"], 1)
```

- [ ] **Step 2: Failing test** `tests/research/test_case_builder.py`

```python
import datetime as dt

import pytest

from tests.research.case_fixtures import complete_result, pre_holdout_result
from tests.research.evaluation_fixtures import CONIDS
from trader.automation.artifact_verifier import ArtifactVerifier, ArtifactVerifierError
from trader.automation.paper_materials import PaperMaterialsError, require_qualified_research_evidence
from trader.research.case_builder import build_failed_case, build_initial_case, evaluation_summary
from trader.research.cohort import CohortSpec
from trader.research.evaluation_case import (FULL_MENU, NO_DEPLOY_MENU, case_path, load_verified_case,
                                             offered_menu, write_evaluation_case)
from trader.research.evaluation_request import EvaluationRequestBody, evaluation_request_id
from trader.research.signing import AttestationSigner

NOW = dt.datetime(2024, 3, 29, 21, tzinfo=dt.timezone.utc)
BODY = EvaluationRequestBody.model_validate({"strategy_key": "strategies/time_of_day.py:TimeOfDay",
                                             "cohort": [{"ENTRY_MINUTE": 600}], "conids": CONIDS,
                                             "bar_size": "15 mins", "research_day": "2024-03-29"})
SPEC = CohortSpec(request_id=evaluation_request_id(BODY), body=BODY, strategy_key=BODY.strategy_key, base=None,
                  cohort=({"ENTRY_MINUTE": 600},), neighbours=(({"ENTRY_MINUTE": 540},),),
                  file_hash="sha256:" + "b" * 64)


def build(result):
    return build_initial_case(SPEC, "2024-03-29", result, created_at=NOW, warmup_sessions=5)


def test_a_rule_failure_is_a_case_without_an_artifact_that_offers_shadow_or_reject():
    case = build(pre_holdout_result())
    assert case.stage == "PRE_HOLDOUT_FAILED" and case.artifact_id is None and case.final_rule_results == []
    assert case.holdout_passed is None and case.evidence["holdout"] is None
    assert offered_menu(case) == NO_DEPLOY_MENU
    assert case.evidence["points"][0]["rules"][0]["observed"] is None         # NaN never reaches the bytes


def test_a_holdout_failure_names_the_decision_and_offers_no_deploy():
    case = build(complete_result(holdout_passed=False))
    assert case.stage == "HOLDOUT_FAILED" and case.eligibility_decision_digest == "d" * 64
    assert case.holdout_passed is False and evaluation_summary(case, order_notional=1900.0)["holdout_passed"] is False
    assert case.evidence["holdout"]["passed"] is False                          # one result, header and evidence
    assert offered_menu(case) == NO_DEPLOY_MENU


def test_a_complete_passing_case_offers_deploy_and_its_summary_matches_the_menu():
    case = build(complete_result())
    assert case.stage == "COMPLETE" and case.holdout_passed is True and offered_menu(case) == FULL_MENU
    assert case.evidence["holdout"]["passed"] is True
    summary = evaluation_summary(case, order_notional=1900.0)
    assert summary["rules_passed"] is True and summary["holdout_passed"] is True
    assert summary["params"] == {"ENTRY_MINUTE": 600} and summary["prior_holdouts"] == 1
    assert summary["previously_revealed_sessions"] == 1 and summary["forward"] is None
    assert (summary["selected_index"], summary["error"]) == (0, None)
    point = summary["points"][0]
    assert point["pre_holdout_passed"] is True and point["metrics"]["expectancy_bps_2x"] == 5.5


def test_a_failure_after_the_claim_is_a_failed_case():
    case = build_failed_case(BODY, request_id=SPEC.request_id, claim_day="2024-03-29", file_hash=SPEC.file_hash,
                             error="EvaluationError: no bars", created_at=NOW, warmup_sessions=5)
    assert case.stage == "FAILED" and case.holdout_passed is None and case.evidence["error"] == "EvaluationError: no bars"
    assert "holdout" in case.evidence and case.evidence["holdout"] is None
    summary = evaluation_summary(case, order_notional=1900.0)
    assert summary["rules_passed"] is False and summary["error"] == "EvaluationError: no bars"
    assert (summary["points"], summary["selected_index"]) == ([], None)


def test_a_written_case_is_never_bundle_evidence(tmp_path):
    signer = AttestationSigner.generate()
    case = build(complete_result())
    digest = write_evaluation_case(tmp_path / "cases", case, signer)
    assert load_verified_case(tmp_path / "cases", digest, {signer.public_key_id: signer.public_key}) == case
    path = case_path(tmp_path / "cases", digest)
    with pytest.raises(ArtifactVerifierError):
        ArtifactVerifier([signer.public_key]).verify(path, "paper", case.artifact_id, NOW)
    for candidate in (path, path.parent):
        with pytest.raises(PaperMaterialsError):
            require_qualified_research_evidence(candidate)
```

- [ ] **Step 3: Run** `.venv/bin/python -m pytest tests/research/test_case_builder.py -q` — expected: FAIL (`ModuleNotFoundError: trader.research.case_builder`).

- [ ] **Step 4: Implement** `trader/research/case_builder.py`

```python
"""Plan 3 fills Plan 1's EvaluationCase (SP2c spec 5.1): the code-built record of one evaluation.

The header is what the trader reads; ``evidence`` carries the detail (every point's rule results, cost stress,
deflated statistic, trial counts, previously revealed sessions, warm-up). A case never authorizes trading.
"""
from __future__ import annotations

import datetime as dt
import math
from typing import Any, Mapping, Optional

from trader.research.evaluation_case import CASE_DOMAIN, FULL_MENU, EvaluationCase, offered_menu
from trader.research.strategy_key import split_strategy_key

STAGE_NAMES = {"pre_holdout": "PRE_HOLDOUT_FAILED", "holdout_failed": "HOLDOUT_FAILED", "complete": "COMPLETE"}


def json_safe(value: Any) -> Any:
    """Numpy scalars to Python, non-finite floats to None: the canonical bytes refuse NaN."""
    if hasattr(value, "item") and not isinstance(value, (list, tuple, dict)):
        value = value.item()
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, Mapping):
        return {str(k): json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_safe(v) for v in value]
    return str(value)


def _point(verdict: Any, decision: Any) -> dict:
    evidence = verdict.evidence
    return {"index": verdict.index, "params": dict(verdict.point.params), "trial_id": verdict.point.trial_id,
            "neighbour_trial_ids": [n.trial_id for n in verdict.neighbours],
            "pre_holdout_passed": bool(verdict.gate.passed), "failed_rules": list(verdict.gate.failed),
            "missing_rules": list(verdict.gate.missing),
            "rules": [{"code": r.code, "passed": bool(r.passed), "observed": json_safe(r.observed)}
                      for r in decision.results],
            "expectancy_bps": {"1x": json_safe(evidence.expectancy_bps_baseline),
                               "1.5x": json_safe(evidence.expectancy_bps_1_5x),
                               "2x": json_safe(evidence.expectancy_bps_2x)},
            "selection_statistic": json_safe(evidence.selection_adjusted_confidence)}


def holdout_evidence(outcome: Any) -> dict | None:
    """Ruling 23: the evidence and the header come from the same holdout result."""
    if outcome is None:
        return None
    return {**json_safe(dict(outcome.window)), "passed": bool(outcome.passed)}


def initial_evidence(result: Any, *, warmup_sessions: int) -> dict:
    outcome, selected = result.holdout, result.selected

    def decision_of(verdict):
        chosen = outcome is not None and selected is not None and verdict.index == selected.index
        return outcome.decision if chosen else verdict.decision

    return {"points": [_point(v, decision_of(v)) for v in result.verdicts],
            "strategy_trials": result.strategy_trials,
            "selected_index": None if selected is None else selected.index, "replay_index": result.replay_index,
            "holdout": holdout_evidence(outcome),
            "previously_revealed": list(result.previously_revealed),
            "holdouts_opened_before": result.holdouts_opened_before, "warmup_sessions": warmup_sessions,
            "error": None}


def build_initial_case(spec: Any, claim_day: str, result: Any, *, created_at: dt.datetime,
                       warmup_sessions: int) -> EvaluationCase:
    outcome, selected = result.holdout, result.selected
    decision = None if outcome is None else outcome.decision          # ruling 22: named even when not recorded
    return EvaluationCase(
        schema_version=CASE_DOMAIN, kind="INITIAL", request_id=spec.request_id, claim_day=claim_day,
        strategy_key=spec.strategy_key, strategy_file_hash=spec.file_hash,
        cohort=[dict(p) for p in spec.cohort], conids=list(spec.body.conids), bar_size=spec.body.bar_size,
        stage=STAGE_NAMES[result.stage], holdout_passed=None if outcome is None else bool(outcome.passed),
        selected_params=None if selected is None else dict(selected.point.params),
        family_id=result.family_id, selected_trial_id=None if selected is None else selected.point.trial_id,
        artifact_id=None if outcome is None else outcome.artifact_id,
        eligibility_decision_digest=None if decision is None else decision.digest,
        decision_state=None if decision is None else decision.state,
        ruleset_digest=None if decision is None else decision.ruleset_digest,
        final_rule_results=[] if decision is None else [{"code": r.code, "passed": bool(r.passed)}
                                                        for r in decision.results],
        renewal=None, created_at=created_at.isoformat(),
        evidence=initial_evidence(result, warmup_sessions=warmup_sessions))


def build_failed_case(body: Any, *, request_id: str, claim_day: str, file_hash: str, error: str,
                      created_at: dt.datetime, warmup_sessions: int) -> EvaluationCase:
    return EvaluationCase(
        schema_version=CASE_DOMAIN, kind="INITIAL", request_id=request_id, claim_day=claim_day,
        strategy_key=body.strategy_key, strategy_file_hash=file_hash, cohort=[dict(p) for p in body.cohort],
        conids=list(body.conids), bar_size=body.bar_size, stage="FAILED", holdout_passed=None,
        selected_params=None, family_id=None,
        selected_trial_id=None, artifact_id=None, eligibility_decision_digest=None, decision_state=None,
        ruleset_digest=None, final_rule_results=[], renewal=None, created_at=created_at.isoformat(),
        evidence={"points": [], "strategy_trials": None, "selected_index": None, "replay_index": 0,
                  "holdout": None, "previously_revealed": [], "holdouts_opened_before": None,
                  "warmup_sessions": warmup_sessions, "error": error[:300]})


def _point_metrics(point: Mapping[str, Any]) -> dict:
    return {"expectancy_bps_1x": point["expectancy_bps"]["1x"], "expectancy_bps_1_5x": point["expectancy_bps"]["1.5x"],
            "expectancy_bps_2x": point["expectancy_bps"]["2x"], "selection_statistic": point["selection_statistic"]}


def _point_summary(point: Mapping[str, Any]) -> dict:
    """Per cohort point, code-computed only (Plan 4 builds Jev's case from it)."""
    return {"index": point["index"], "params": point["params"], "pre_holdout_passed": point["pre_holdout_passed"],
            "failed_rules": point["failed_rules"], "missing_rules": point["missing_rules"],
            "metrics": _point_metrics(point)}


def evaluation_summary(case: EvaluationCase, *, order_notional: float) -> dict:
    """Plan 4's EvaluationSummary: the code-computed view Jev's BacktestCase is built from."""
    evidence = case.evidence
    path, class_name = split_strategy_key(case.strategy_key)
    points = evidence.get("points") or []
    shown: Optional[dict] = points[evidence["replay_index"]] if points else None
    return {
        "kind": case.kind, "strategy_key": case.strategy_key, "strategy_path": path, "class_name": class_name,
        "file_hash": case.strategy_file_hash, "params": case.selected_params, "conids": list(case.conids),
        "bar_size": case.bar_size, "stage": case.stage, "rules_passed": offered_menu(case) == FULL_MENU,
        "holdout_passed": case.holdout_passed,                      # the typed header (ruling 23)
        "eligibility": case.decision_state, "renewal_checks_passed": None, "prior_version_digest": None,
        "order_notional": float(order_notional), "strategy_trials": evidence.get("strategy_trials") or 0,
        "prior_holdouts": evidence.get("holdouts_opened_before") or 0,
        "previously_revealed_sessions": len(evidence.get("previously_revealed") or []),
        "metrics": {} if shown is None else _point_metrics(shown),
        "rule_results": [{"point": p["index"], "rule": r["code"], "passed": r["passed"]}
                         for p in points for r in p["rules"]],
        "selected_index": evidence.get("selected_index"), "error": evidence.get("error"),
        "points": [_point_summary(p) for p in points],
        "forward": None}
```

- [ ] **Step 5: Run** `.venv/bin/python -m pytest tests/research/test_case_builder.py -q` — expected: PASS.

- [ ] **Step 6: Commit** `feat: build signed evaluation cases from cohort results`

```
feat: build signed evaluation cases from cohort results

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
```

### Task 6: The evaluation service (claim first, then queue; lost reply; restart)

**Files:** Create `trader/research/service_store.py`, `trader/research/trader_port.py`, `trader/research/evaluation_service.py`, `tests/research/service_fakes.py`, `tests/research/test_evaluation_service.py`. Modify `trader/research/schema.py` (call `apply_service_migrations` last in `apply_research_migrations`).

**Interfaces.**
- Consumes Plan 1's `claim_evaluation`, `get_evaluation_claim`, `update_evaluation_claim`, `EvaluationRequestBody`, `write_evaluation_case`, `load_verified_case`; Task 3 `request_body`, `RequestRefused`; Task 5 `build_initial_case`, `build_failed_case`, `evaluation_summary`.
- Produces `TraderUnavailable`, `TraderPort(query_client, command_client)` with `claim(request_id, body) -> dict`, `claim_readback(request_id) -> Optional[dict]`, `update_claim(request_id, state) -> dict`, `judgment(*, judgment_id=None, case_digest=None) -> Optional[dict]`, `record_shadow(body) -> dict`.
- Produces `ResearchStore(db)` and `EvaluationService(*, store, trader, build_spec, evaluate, signer, artifacts_root, warmup_sessions, order_notional, queue_max, now)` with `submit(raw, caller) -> dict`, `get(request_id, caller) -> dict`, `recover() -> None`, `report_pending() -> bool`, `run_next() -> bool` (one tick: owed reports first, then one queued evaluation; true when anything moved), `serve_forever(stop)`. `ResearchStore` rows carry `pending_report`; `hold_report(...)`, `confirm_report(...)`, `pending_reports()` (ruling 24). `build_spec(body: EvaluationRequestBody) -> CohortSpec`.

- [ ] **Step 1: Fakes** `tests/research/service_fakes.py`

```python
"""An in-memory trader for the research service tests: Plan 1's claim and judgment shapes, lost replies."""
from types import SimpleNamespace

from trader.research.trader_port import TraderUnavailable


class FakeTrader:
    def __init__(self, *, limit=10, cooling=()):
        self.claims, self.updates, self.limit, self.cooling = {}, [], limit, set(cooling)
        self.lose_next_reply = False
        self.lose_update = {}                 # state -> "request" (never arrives) or "reply" (applied, answer lost)
        self.judgments, self.shadow_rows = {}, {}
        self.shadow_calls, self.refuse_shadow = [], {}    # (judgment_id, session_date) -> (code, retryable)

    def claim(self, request_id, body):
        existing = self.claims.get(request_id)
        if existing is not None:
            return {"status": "EXISTING", "claim": dict(existing), "code": None, "detail": None, "retryable": False}
        if body["strategy_key"] in self.cooling:
            return {"status": "REFUSED", "claim": None, "code": "FAMILY_COOLING_DOWN", "detail": "cooling",
                    "retryable": False}
        if len(self.claims) >= self.limit:
            return {"status": "REFUSED", "claim": None, "code": "EVALUATION_LIMIT_REACHED", "detail": "limit",
                    "retryable": False}
        self.claims[request_id] = {"request_id": request_id, "strategy_key": body["strategy_key"],
                                   "ny_day": body["research_day"], "state": "QUEUED", "body": dict(body),
                                   "claimed_at": "2024-03-29T14:00:00+00:00",
                                   "updated_at": "2024-03-29T14:00:00+00:00"}
        if self.lose_next_reply:
            self.lose_next_reply = False
            raise TraderUnavailable("reply lost")
        return {"status": "ACCEPTED", "claim": dict(self.claims[request_id]), "code": None, "detail": None,
                "retryable": False}

    def claim_readback(self, request_id):
        claim = self.claims.get(request_id)
        return None if claim is None else dict(claim)

    def update_claim(self, request_id, state):
        self.updates.append((request_id, state))
        lost = self.lose_update.pop(state, None)
        if lost == "request":
            raise TraderUnavailable("update_claim: request lost")
        claim = self.claims[request_id]
        status = "UNCHANGED" if claim["state"] == state else "UPDATED"
        claim["state"] = state
        if lost == "reply":
            raise TraderUnavailable("update_claim: reply lost")
        return {"status": status, "code": None, "detail": None, "retryable": False}

    def judgment(self, *, judgment_id=None, case_digest=None):
        found = [j for j in self.judgments.values()
                 if j["judgment_id"] == judgment_id or j["case_digest"] == case_digest]
        return dict(found[0]) if found else None

    def record_shadow(self, body):
        key = (body["judgment_id"], body["session_date"])
        self.shadow_calls.append(key)
        if key in self.refuse_shadow:
            code, retryable = self.refuse_shadow[key]
            return {"status": "REFUSED", "code": code, "detail": "refused by the fake", "retryable": retryable}
        if key in self.shadow_rows and self.shadow_rows[key] != body:
            return {"status": "REFUSED", "code": "CONFLICTING_DUPLICATE", "retryable": False}
        status = "DUPLICATE" if key in self.shadow_rows else "INSERTED"
        self.shadow_rows[key] = dict(body)
        return {"status": status, "code": None, "retryable": False}


def judgment_view(judgment_id, case_digest, verdict, *, decided_at="2024-03-15T21:00:00+00:00",
                  narrative=None, binding=None, kind="INITIAL", jev_model="openrouter/jev-1"):
    """Plan 1's get_backtest_judgment view."""
    return {"judgment_id": judgment_id, "case_digest": case_digest, "request_id": None, "kind": kind,
            "verdict": verdict, "strategy_key": "strategies/time_of_day.py:TimeOfDay",
            "body": {"judgment_id": judgment_id, "case_digest": case_digest, "kind": kind, "verdict": verdict,
                     "jev_model": jev_model, "decided_at": decided_at, "narrative": narrative},
            "binding": binding or {}, "cooldown_until_session": None, "recorded_at": decided_at}


AI = SimpleNamespace(principal="ai_research")
CLI = SimpleNamespace(principal="cli")
```

- [ ] **Step 2: Failing test** `tests/research/test_evaluation_service.py`

```python
import datetime as dt
from types import SimpleNamespace

import pytest

from tests.research.case_fixtures import pre_holdout_result
from tests.research.evaluation_fixtures import CONIDS, build_spec_file, write_costs_config, write_universe
from tests.research.service_fakes import AI, CLI, FakeTrader
from trader.automation.backtest_judge_config import BacktestJudgeConfig
from trader.data.duckdb_store import DuckDBConnection
from trader.data.schema_migrations import SchemaMigrator
from trader.data.universe import UniverseAccessor
from trader.research.artifact import ExperimentFamily
from trader.research.cohort import build_cohort_spec
from trader.research.evaluation import EvaluationError
from trader.research.evaluation_case import load_verified_case
from trader.research.evaluation_service import EvaluationService
from trader.research.experiment_registry import ExperimentRegistry
from trader.research.schema import apply_research_migrations
from trader.research.service_config import ResearchServiceConfig
from trader.research.service_store import ResearchStore
from trader.research.signing import AttestationSigner
from trader.simulation.execution_costs import load_execution_costs_config

KEY = "strategies/time_of_day.py:TimeOfDay"
NOW = dt.datetime(2024, 3, 29, 14, tzinfo=dt.timezone.utc)          # 10:00 New York, 2024-03-29


def request(**overrides):
    return {"kind": "INITIAL", "strategy_key": KEY, "cohort": [{"ENTRY_MINUTE": 600}], "conids": CONIDS,
            "bar_size": "15 mins", **overrides}


def fake_evaluate(registry):
    def _evaluate(spec):
        fid = registry.create_family(ExperimentFamily(
            strategy_path="strategies/time_of_day.py", class_name="TimeOfDay", repository_commit="unknown",
            source_tree_digest="s", dependency_lock_digest="d", container_digest="unpinned",
            dataset_manifest_digest="m", search_space={"request": spec.request_id}, cost_model={},
            validation_protocol={}), created_at=NOW)
        for key in ("main", "n1", "n2"):
            tid = registry.start_trial(fid, trial_key=key, parameters={}, started_at=NOW)
            registry.finish_trial(tid, status="SUCCEEDED", finished_at=NOW, metrics={"daily_sharpe": 0.1})
        return pre_holdout_result(spec.cohort)
    return _evaluate


@pytest.fixture
def world(tmp_path, tmp_duckdb_path):
    write_universe(tmp_duckdb_path)
    build_spec_file(tmp_path)
    costs = load_execution_costs_config(str(write_costs_config(tmp_path / "execution_costs.yaml")))
    db = DuckDBConnection.get_instance(str(tmp_path / "research.duckdb"))
    apply_research_migrations(SchemaMigrator(db))
    registry = ExperimentRegistry(db)
    config = ResearchServiceConfig(period_sessions=38, folds=2, embargo_sessions=1, holdout_sessions=5)
    judge = BacktestJudgeConfig(strategy_allowlist=(KEY,), max_cohort_points=3)
    trader, signer = FakeTrader(), AttestationSigner.generate()

    def build_spec(body):
        return build_cohort_spec(body, config=config, judge=judge,
                                 universe_accessor=UniverseAccessor(tmp_duckdb_path, "Universes"),
                                 costs_config=costs, repo_root=tmp_path, registry=registry)

    def service(evaluate=None):
        return EvaluationService(store=ResearchStore(db), trader=trader, build_spec=build_spec,
                                 evaluate=evaluate or fake_evaluate(registry), signer=signer,
                                 artifacts_root=tmp_path / "artifacts", warmup_sessions=5, order_notional=1900.0,
                                 queue_max=2, now=lambda: NOW)
    return SimpleNamespace(service=service, trader=trader, registry=registry, signer=signer, root=tmp_path)


def trials(world):
    return world.registry.strategy_trials("strategies/time_of_day.py", "TimeOfDay")


def case_of(world, view):
    return load_verified_case(world.root / "artifacts" / "cases", view["case_digest"],
                              {world.signer.public_key_id: world.signer.public_key})


def test_a_refused_claim_runs_nothing_and_writes_no_trial(world):
    world.trader.cooling.add(KEY)
    service = world.service()
    reply = service.submit(request(), AI)
    assert (reply["status"], reply["code"], reply["retryable"]) == ("REFUSED", "FAMILY_COOLING_DOWN", False)
    assert service.run_next() is False and trials(world) == []


def test_an_unreachable_trader_is_a_retryable_refusal_that_claims_nothing(world, monkeypatch):
    from trader.research.trader_port import TraderUnavailable

    def down(*args, **kwargs):
        raise TraderUnavailable("no route to the trader")
    monkeypatch.setattr(world.trader, "claim", down)
    monkeypatch.setattr(world.trader, "claim_readback", down)
    reply = world.service().submit(request(), AI)
    assert (reply["status"], reply["code"], reply["retryable"]) == ("REFUSED", "CLAIM_UNKNOWN", True)
    assert world.trader.claims == {}


def test_the_daily_limit_is_the_traders(world):
    world.trader.limit = 1
    service = world.service()
    assert service.submit(request(), AI)["status"] == "ACCEPTED"
    second = service.submit(request(cohort=[{"ENTRY_MINUTE": 615}]), AI)
    assert (second["status"], second["code"]) == ("REFUSED", "EVALUATION_LIMIT_REACHED")


def test_a_lost_claim_reply_is_read_back_and_takes_one_slot(world):
    world.trader.lose_next_reply = True
    service = world.service()
    assert service.submit(request(), AI)["state"] == "QUEUED"
    retry = service.submit(request(), AI)
    assert (retry["status"], retry["state"]) == ("DUPLICATE", "QUEUED")      # same id, no second slot
    assert len(world.trader.claims) == 1


def test_restart_resumes_under_the_same_claim_and_an_outage_wrote_no_trial(world):
    request_id = world.service().submit(request(), AI)["request_id"]
    assert trials(world) == []                                            # outage before any trial
    restarted = world.service()
    restarted.recover()
    assert restarted.run_next() is True
    view = restarted.get(request_id, CLI)
    assert view["found"] and view["state"] == "DONE" and len(trials(world)) == 3
    assert len(world.trader.claims) == 1
    assert world.trader.updates == [(request_id, "RUNNING"), (request_id, "DONE")]
    case = case_of(world, view)
    assert case.stage == "PRE_HOLDOUT_FAILED" and case.evidence["points"][0]["rules"][0]["observed"] is None
    assert "bundle_digest" not in view and view["summary"]["rules_passed"] is False


def test_holdout_not_available_is_refused_before_any_claim(world, monkeypatch):
    monkeypatch.setattr(world.registry, "opened_holdout_windows", lambda path, cls: [
        {"artifact_id": "a", "family_id": "x", "start": dt.date(2024, 3, 20), "end": dt.date(2024, 3, 27)}])
    reply = world.service().submit(request(), AI)
    assert reply["code"] == "HOLDOUT_NOT_AVAILABLE" and world.trader.claims == {}


def test_callers_queue_and_renewal(world):
    service = world.service()
    assert service.submit(request(), CLI)["code"] == "PRINCIPAL_FORBIDDEN"
    renewal = service.submit({"kind": "RENEWAL", "prior_version_digest": "sha256:" + "9" * 64}, AI)
    assert renewal["code"] == "RENEWAL_NOT_SUPPORTED" and world.trader.claims == {}
    service.submit(request(), AI)
    service.submit(request(cohort=[{"ENTRY_MINUTE": 615}]), AI)
    third = service.submit(request(cohort=[{"ENTRY_MINUTE": 630}]), AI)
    assert third["code"] == "QUEUE_FULL" and len(world.trader.claims) == 2


def test_a_failed_run_signs_a_failed_case_and_fails_the_claim(world):
    def boom(spec):
        raise EvaluationError("no 15 mins bars for conids [1001]")
    service = world.service(evaluate=boom)
    request_id = service.submit(request(), AI)["request_id"]
    service.run_next()
    view = service.get(request_id, AI)
    assert view["state"] == "FAILED" and world.trader.updates[-1] == (request_id, "FAILED")
    assert case_of(world, view).evidence["error"].startswith("EvaluationError")


def test_a_changed_strategy_file_is_a_failed_case(world):
    service = world.service()
    request_id = service.submit(request(), AI)["request_id"]
    path = world.root / "strategies" / "time_of_day.py"
    path.write_text(path.read_text() + "\n# changed\n")
    service.run_next()
    assert case_of(world, service.get(request_id, AI)).evidence["error"].startswith("STRATEGY_SOURCE_CHANGED")


def test_a_lost_terminal_report_withholds_the_case_until_the_next_tick_confirms_it(world):  # PR #91 4218218688
    service = world.service()
    request_id = service.submit(request(), AI)["request_id"]
    world.trader.lose_update["DONE"] = "request"                       # the DONE never reaches the trader
    assert service.run_next() is True
    assert world.trader.claims[request_id]["state"] == "RUNNING"
    held = service.get(request_id, AI)
    assert (held["state"], held["case_digest"], held["summary"]) == ("RUNNING", None, None)
    assert service.run_next() is True                                   # the next tick, both services up
    assert world.trader.claims[request_id]["state"] == "DONE"
    view = service.get(request_id, AI)
    assert view["state"] == "DONE" and view["case_digest"] is not None and view["summary"] is not None
    assert world.trader.updates == [(request_id, "RUNNING"), (request_id, "DONE"), (request_id, "DONE")]
    assert service.run_next() is False                                  # nothing is owed any more


def test_a_lost_terminal_reply_is_confirmed_by_reading_the_claim_back(world):
    service = world.service()
    request_id = service.submit(request(), AI)["request_id"]
    world.trader.lose_update["DONE"] = "reply"                          # applied at the trader, the answer lost
    service.run_next()
    assert service.get(request_id, AI)["state"] == "DONE"
    assert world.trader.updates == [(request_id, "RUNNING"), (request_id, "DONE")]   # read back, not sent again
```

- [ ] **Step 3: Run** `.venv/bin/python -m pytest tests/research/test_evaluation_service.py -q` — expected: FAIL (`ModuleNotFoundError: trader.research.trader_port`).

- [ ] **Step 4: Implement** `trader/research/service_store.py`

```python
"""Research DB migrations 20-22: the service's requests, its signed cases, the shadow cohort (SP2c Plan 3)."""
from __future__ import annotations

import datetime as dt
import json
from typing import Any, Optional

RESEARCH_MIGRATION_REQUESTS = 20
RESEARCH_MIGRATION_CASES = 21
RESEARCH_MIGRATION_SHADOW = 22
REQUEST_STATES = ("CLAIMING", "REFUSED", "QUEUED", "RUNNING", "DONE", "FAILED")
OPEN_STATES = ("QUEUED", "RUNNING")

_REQUESTS = ("""CREATE TABLE IF NOT EXISTS research_requests (
    request_id VARCHAR PRIMARY KEY, body_json VARCHAR NOT NULL, strategy_key VARCHAR NOT NULL,
    file_hash VARCHAR NOT NULL, state VARCHAR NOT NULL
        CHECK (state IN ('CLAIMING','REFUSED','QUEUED','RUNNING','DONE','FAILED')),
    ny_day DATE, code VARCHAR, detail VARCHAR, case_digest VARCHAR, summary_json VARCHAR,
    pending_report VARCHAR CHECK (pending_report IS NULL OR pending_report IN ('DONE','FAILED')),
    created_at TIMESTAMPTZ NOT NULL, updated_at TIMESTAMPTZ NOT NULL)""",)
_CASES = ("""CREATE TABLE IF NOT EXISTS research_cases (
    case_digest VARCHAR PRIMARY KEY, request_id VARCHAR NOT NULL UNIQUE, stage VARCHAR NOT NULL,
    signed_at TIMESTAMPTZ NOT NULL)""",)
_SHADOW = (
    """CREATE TABLE IF NOT EXISTS shadow_members (
        judgment_id VARCHAR PRIMARY KEY, case_digest VARCHAR NOT NULL UNIQUE, verdict VARCHAR NOT NULL
            CHECK (verdict IN ('DEPLOY','SHADOW','REJECT')),
        decided_at TIMESTAMPTZ NOT NULL, first_session DATE NOT NULL, last_session DATE NOT NULL,
        joined_at TIMESTAMPTZ NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS shadow_sent (
        judgment_id VARCHAR NOT NULL, session_date DATE NOT NULL, status VARCHAR NOT NULL,
        reply_status VARCHAR NOT NULL CHECK (reply_status IN ('INSERTED','DUPLICATE')),
        sent_at TIMESTAMPTZ NOT NULL, PRIMARY KEY (judgment_id, session_date))""",
    """CREATE TABLE IF NOT EXISTS shadow_failures (
        judgment_id VARCHAR NOT NULL, session_date DATE NOT NULL, code VARCHAR NOT NULL, detail VARCHAR,
        failed_at TIMESTAMPTZ NOT NULL, PRIMARY KEY (judgment_id, session_date))""",
)


def apply_service_migrations(migrator: Any) -> None:
    migrator.apply(version=RESEARCH_MIGRATION_REQUESTS, name="research_service_requests", statements=list(_REQUESTS))
    migrator.apply(version=RESEARCH_MIGRATION_CASES, name="research_service_cases", statements=list(_CASES))
    migrator.apply(version=RESEARCH_MIGRATION_SHADOW, name="research_service_shadow", statements=list(_SHADOW))


_COLUMNS = ("request_id", "body_json", "strategy_key", "file_hash", "state", "ny_day", "code", "detail",
            "case_digest", "summary_json", "pending_report", "created_at", "updated_at")


class ResearchStore:
    def __init__(self, db: Any):
        self._db = db

    @staticmethod
    def _row(values) -> dict:
        row = dict(zip(_COLUMNS, values))
        row["body"] = json.loads(row.pop("body_json"))
        summary = row.pop("summary_json")
        row["summary"] = None if summary is None else json.loads(summary)
        return row

    def get(self, request_id: str) -> Optional[dict]:
        found = self._db.execute(f"SELECT {', '.join(_COLUMNS)} FROM research_requests WHERE request_id = ?",
                                 [request_id], fetch="one")
        return None if found is None else self._row(found)

    def begin(self, request_id: str, body: dict, strategy_key: str, file_hash: str, now: dt.datetime) -> None:
        """Insert a CLAIMING row, or move a REFUSED/CLAIMING row back to CLAIMING for another claim."""
        def tx(conn):
            if conn.execute("SELECT 1 FROM research_requests WHERE request_id = ?", [request_id]).fetchone():
                conn.execute("UPDATE research_requests SET state = 'CLAIMING', code = NULL, detail = NULL, "
                             "file_hash = ?, updated_at = ? WHERE request_id = ? AND state IN ('CLAIMING','REFUSED')",
                             [file_hash, now, request_id])
                return
            conn.execute("INSERT INTO research_requests (request_id, body_json, strategy_key, file_hash, state, "
                         "created_at, updated_at) VALUES (?, ?, ?, ?, 'CLAIMING', ?, ?)",
                         [request_id, json.dumps(body, sort_keys=True), strategy_key, file_hash, now, now])
        self._db.transaction(tx)

    def set_state(self, request_id: str, state: str, *, now: dt.datetime, ny_day=None, code=None, detail=None,
                  case_digest=None, summary=None) -> None:
        if state not in REQUEST_STATES:
            raise ValueError(f"unknown request state {state!r}")
        self._db.execute(
            "UPDATE research_requests SET state = ?, ny_day = COALESCE(?, ny_day), code = ?, detail = ?, "
            "case_digest = COALESCE(?, case_digest), summary_json = COALESCE(?, summary_json), updated_at = ? "
            "WHERE request_id = ?",
            [state, ny_day, code, detail, case_digest, None if summary is None else json.dumps(summary), now,
             request_id])

    def count(self, states) -> int:
        marks = ", ".join("?" for _ in states)
        return int(self._db.execute(f"SELECT COUNT(*) FROM research_requests WHERE state IN ({marks})",
                                    list(states), fetch="one")[0])

    def _select(self, where: str) -> list[dict]:
        rows = self._db.execute(f"SELECT {', '.join(_COLUMNS)} FROM research_requests WHERE {where} "
                                "ORDER BY created_at, request_id", fetch="all")
        return [self._row(r) for r in rows]

    def pending(self) -> list[dict]:
        return self._select("state IN ('CLAIMING','QUEUED','RUNNING')")

    def finished(self) -> list[dict]:
        return self._select("state IN ('DONE','FAILED')")

    def hold_report(self, request_id: str, final: str, *, now: dt.datetime, case_digest: str, summary: dict) -> None:
        """Ruling 24: the case is signed, but the row stays RUNNING until the trader confirms ``final``."""
        if final not in ("DONE", "FAILED"):
            raise ValueError(f"a report owes DONE or FAILED, not {final!r}")
        self._db.execute("UPDATE research_requests SET pending_report = ?, case_digest = ?, summary_json = ?, "
                         "updated_at = ? WHERE request_id = ? AND state = 'RUNNING'",
                         [final, case_digest, json.dumps(summary), now, request_id])

    def confirm_report(self, request_id: str, *, now: dt.datetime) -> None:
        self._db.execute("UPDATE research_requests SET state = pending_report, pending_report = NULL, updated_at = ? "
                         "WHERE request_id = ? AND pending_report IS NOT NULL", [now, request_id])

    def pending_reports(self) -> list[dict]:
        return self._select("pending_report IS NOT NULL")

    def record_case(self, case_digest: str, request_id: str, stage: str, signed_at: dt.datetime) -> None:
        def tx(conn):
            if conn.execute("SELECT 1 FROM research_cases WHERE case_digest = ?", [case_digest]).fetchone() is None:
                conn.execute("INSERT INTO research_cases VALUES (?, ?, ?, ?)",
                             [case_digest, request_id, stage, signed_at])
        self._db.transaction(tx)

    def case(self, *, case_digest: Optional[str] = None, request_id: Optional[str] = None) -> Optional[dict]:
        column, value = ("case_digest", case_digest) if case_digest else ("request_id", request_id)
        found = self._db.execute(f"SELECT case_digest, request_id, stage, signed_at FROM research_cases "
                                 f"WHERE {column} = ?", [value], fetch="one")
        return None if found is None else dict(zip(("case_digest", "request_id", "stage", "signed_at"), found))
```

`trader/research/trader_port.py`

```python
"""The research service's signed calls to the trader (spec 5.1: research may call these methods only)."""
from __future__ import annotations

from typing import Any, Optional

from trader.messaging.typed_rpc import TypedRpcError, TypedRpcRemoteError

TIMEOUT_SECONDS = 15.0


class TraderUnavailable(Exception):
    """The call did not complete: the trader may or may not have acted (read back before any retry)."""


class TraderPort:
    def __init__(self, query_client: Any, command_client: Any):
        self._query, self._command = query_client, command_client

    @staticmethod
    def _call(client: Any, method: str, body: dict) -> dict:
        try:
            return client.call(method, body, dict, timeout=TIMEOUT_SECONDS)
        except TypedRpcRemoteError:
            raise                                   # a typed transport refusal is a bug here: fail loudly
        except (ConnectionError, TimeoutError, OSError, TypedRpcError) as exc:
            raise TraderUnavailable(f"{method}: {type(exc).__name__}") from exc

    def claim(self, request_id: str, body: dict) -> dict:
        return self._call(self._command, "claim_evaluation", {"request_id": request_id, "body": body})

    def claim_readback(self, request_id: str) -> Optional[dict]:
        return self._call(self._query, "get_evaluation_claim", {"request_id": request_id})["claim"]

    def update_claim(self, request_id: str, state: str) -> dict:
        return self._call(self._command, "update_evaluation_claim", {"request_id": request_id, "state": state})

    def judgment(self, *, judgment_id: Optional[str] = None, case_digest: Optional[str] = None) -> Optional[dict]:
        """Plan 1's view, by judgment id or by case digest (Plan 1 sends both keys, exactly one set)."""
        if (judgment_id is None) == (case_digest is None):
            raise ValueError("name exactly one of judgment_id or case_digest")
        body = {"judgment_id": judgment_id, "case_digest": case_digest}
        return self._call(self._query, "get_backtest_judgment", body)["judgment"]

    def record_shadow(self, body: dict) -> dict:
        return self._call(self._command, "record_shadow_result", body)
```

`trader/research/evaluation_service.py`

```python
"""submit_evaluation / get_evaluation (SP2c spec 5.1): claim at the trader first, then queue; one worker."""
from __future__ import annotations

import datetime as dt
import logging
import queue
import threading
from pathlib import Path
from typing import Any, Callable, Mapping, Optional
from zoneinfo import ZoneInfo

from trader.research.case_builder import build_failed_case, build_initial_case, evaluation_summary
from trader.research.cohort import RequestRefused, request_body
from trader.research.evaluation_case import load_verified_case, write_evaluation_case
from trader.research.evaluation_request import EvaluationRequestBody
from trader.research.service_store import OPEN_STATES
from trader.research.trader_port import TraderUnavailable

logger = logging.getLogger(__name__)
NEW_YORK = ZoneInfo("America/New_York")
AI_RESEARCH = "ai_research"
READERS = frozenset({"ai_research", "cli"})
FINISHED_CLAIM_STATES = ("DONE", "FAILED")


class ServiceStateError(RuntimeError):
    """The research DB and the trader's claims disagree; the operator must look."""


def _submit_reply(status: str, request_id: Optional[str] = None, state: Optional[str] = None,
                  code: Optional[str] = None, detail: Optional[str] = None, retryable: bool = False) -> dict:
    """``retryable``: the caller may resend the same body later (nothing was decided)."""
    return {"status": status, "request_id": request_id, "state": state, "code": code, "detail": detail,
            "retryable": retryable}


class EvaluationService:
    def __init__(self, *, store: Any, trader: Any, build_spec: Callable[[EvaluationRequestBody], Any],
                 evaluate: Callable[[Any], Any], signer: Any, artifacts_root: Path, warmup_sessions: int,
                 order_notional: float, queue_max: int, now: Callable[[], dt.datetime]):
        self._store, self._trader, self._build_spec, self._evaluate = store, trader, build_spec, evaluate
        self._signer, self._cases_dir = signer, Path(artifacts_root) / "cases"
        self._warmup_sessions, self._order_notional = warmup_sessions, order_notional
        self._queue_max, self._now = queue_max, now
        self._queue: "queue.Queue[str]" = queue.Queue()
        self._submit_lock = threading.Lock()

    def _today(self) -> dt.date:
        return self._now().astimezone(NEW_YORK).date()

    # -- submit_evaluation ------------------------------------------------------------

    def submit(self, raw: Mapping[str, Any], caller: Any) -> dict:
        if caller.principal != AI_RESEARCH:
            return _submit_reply("REFUSED", code="PRINCIPAL_FORBIDDEN", detail="only ai_research submits")
        if raw.get("kind") != "INITIAL":
            return _submit_reply("REFUSED", code="RENEWAL_NOT_SUPPORTED", detail="renewal cases: SP2c Plan 5")
        try:
            body = request_body({k: v for k, v in raw.items() if k != "kind"}, self._today())
            spec = self._build_spec(body)
        except RequestRefused as refusal:
            return _submit_reply("REFUSED", code=refusal.code, detail=refusal.detail)   # nothing is claimed
        with self._submit_lock:
            row = self._store.get(spec.request_id)
            if row is not None and row["state"] not in ("CLAIMING", "REFUSED"):
                return _submit_reply("DUPLICATE", spec.request_id, row["state"])        # a retry
            if row is None and self._store.count(OPEN_STATES) >= self._queue_max:
                return _submit_reply("REFUSED", code="QUEUE_FULL",
                                     detail=f"{self._queue_max} evaluations are already waiting")
            self._store.begin(spec.request_id, body.model_dump(), spec.strategy_key, spec.file_hash, self._now())
            try:
                reply = self._claim(spec.request_id, body.model_dump())
            except TraderUnavailable as exc:
                return _submit_reply("REFUSED", spec.request_id, code="CLAIM_UNKNOWN", detail=str(exc),
                                     retryable=True)
            return self._accept(spec.request_id, reply)

    def _claim(self, request_id: str, body: dict) -> dict:
        """Spec 5.1 step 5: after a lost reply, read the claim back before any retry."""
        try:
            return self._trader.claim(request_id, body)
        except TraderUnavailable:
            claim = self._trader.claim_readback(request_id)
            if claim is not None:
                return {"status": "EXISTING", "claim": claim, "code": None, "detail": None}
            return self._trader.claim(request_id, body)   # same id and body: never a second slot

    def _accept(self, request_id: str, reply: dict) -> dict:
        if reply["status"] == "REFUSED":
            self._store.set_state(request_id, "REFUSED", now=self._now(), code=reply["code"], detail=reply["detail"])
            return _submit_reply("REFUSED", request_id, code=reply["code"], detail=reply["detail"],
                                 retryable=bool(reply.get("retryable")))
        claim = reply["claim"]
        if claim is None or claim["request_id"] != request_id:
            raise ServiceStateError(f"the trader answered {reply['status']} without the claim for {request_id}")
        if claim["state"] in FINISHED_CLAIM_STATES:
            self._store.set_state(request_id, "FAILED", now=self._now(),
                                  ny_day=dt.date.fromisoformat(claim["ny_day"]), code="CLAIM_ALREADY_FINISHED",
                                  detail="the trader already closed this claim")
            return _submit_reply("REFUSED", request_id, "FAILED", "CLAIM_ALREADY_FINISHED")
        self._store.set_state(request_id, "QUEUED", now=self._now(), ny_day=dt.date.fromisoformat(claim["ny_day"]))
        self._queue.put(request_id)
        return _submit_reply("ACCEPTED", request_id, "QUEUED")

    # -- get_evaluation ----------------------------------------------------------------

    def get(self, request_id: str, caller: Any) -> dict:
        row = self._store.get(request_id) if caller.principal in READERS else None
        if row is None:
            return {"found": False, "request_id": request_id, "state": None, "case_digest": None, "summary": None}
        if row["pending_report"] is not None:                 # ruling 24: the trader's claim is not closed yet
            return {"found": True, "request_id": request_id, "state": "RUNNING", "case_digest": None, "summary": None}
        return {"found": True, "request_id": request_id, "state": row["state"], "case_digest": row["case_digest"],
                "summary": row["summary"]}

    # -- restart and the worker --------------------------------------------------------

    def recover(self) -> None:
        """Spec 5.1 step 6: resume our QUEUED and RUNNING work under the same claim; report finished states."""
        for row in self._store.pending():
            if row["pending_report"] is not None:
                continue                                      # signed already: only its report is owed (below)
            claim = self._trader.claim_readback(row["request_id"])
            if claim is None:
                if row["state"] == "CLAIMING":
                    continue                                  # never accepted; the caller's retry claims again
                raise ServiceStateError(f"{row['request_id']} is {row['state']} here but the trader has no claim")
            if row["state"] == "CLAIMING":
                self._accept(row["request_id"], {"status": "EXISTING", "claim": claim})
            else:
                self._queue.put(row["request_id"])
        self.report_pending()

    def report_pending(self) -> bool:
        """Ruling 24: send every owed DONE/FAILED again; true when the trader confirmed at least one."""
        confirmed = False
        for row in self._store.pending_reports():
            confirmed |= self._report_end(row["request_id"], row["pending_report"])
        return confirmed

    def run_next(self) -> bool:
        """One service tick: owed reports first (no restart needed), then one queued evaluation."""
        reported = self.report_pending()
        try:
            request_id = self._queue.get_nowait()
        except queue.Empty:
            return reported
        self._run(request_id)
        return True

    def serve_forever(self, stop: threading.Event, idle_seconds: float = 1.0) -> None:
        while not stop.is_set():
            if not self.run_next():
                stop.wait(idle_seconds)

    def _run(self, request_id: str) -> None:
        row = self._store.get(request_id)
        if row["state"] not in OPEN_STATES or row["pending_report"] is not None:
            return
        signed = self._store.case(request_id=request_id)
        if signed is None:
            self._store.set_state(request_id, "RUNNING", now=self._now())
            self._report_running(request_id)
            digest, case = self._evaluate_and_sign(row)
        else:                                                   # signed before a crash: never run twice
            digest = signed["case_digest"]
            case = load_verified_case(self._cases_dir, digest, {self._signer.public_key_id: self._signer.public_key})
        final = "FAILED" if case.stage == "FAILED" else "DONE"
        if row["state"] == "QUEUED":
            self._store.set_state(request_id, "RUNNING", now=self._now())
        self._store.hold_report(request_id, final, now=self._now(), case_digest=digest,
                                summary=evaluation_summary(case, order_notional=self._order_notional))
        self._report_end(request_id, final)

    def _evaluate_and_sign(self, row: dict) -> tuple[str, Any]:
        created_at = self._now()
        body = EvaluationRequestBody.model_validate(row["body"])
        claim_day = str(row["ny_day"])

        def failed(error: str):
            return build_failed_case(body, request_id=row["request_id"], claim_day=claim_day,
                                     file_hash=row["file_hash"], error=error, created_at=created_at,
                                     warmup_sessions=self._warmup_sessions)
        try:
            spec = self._build_spec(body)
            if spec.file_hash != row["file_hash"]:
                raise RequestRefused("STRATEGY_SOURCE_CHANGED", "the strategy file changed after the claim")
            case = build_initial_case(spec, claim_day, self._evaluate(spec), created_at=created_at,
                                      warmup_sessions=self._warmup_sessions)
        except RequestRefused as refusal:
            case = failed(f"{refusal.code}: {refusal.detail}")
        except Exception as exc:                                # recorded, logged, never swallowed silently
            logger.exception("evaluation %s failed", row["request_id"])
            case = failed(f"{type(exc).__name__}: {str(exc)[:200]}")
        digest = write_evaluation_case(self._cases_dir, case, self._signer)
        self._store.record_case(digest, row["request_id"], case.stage, created_at)
        return digest, case

    def _report_running(self, request_id: str) -> None:
        """Best effort: a lost RUNNING blocks nothing (Plan 1 allows QUEUED -> DONE)."""
        try:
            self._trader.update_claim(request_id, "RUNNING")
        except TraderUnavailable:
            logger.warning("claim %s: RUNNING not reported; the end report follows anyway", request_id)

    def _report_end(self, request_id: str, final: str) -> bool:
        """Ruling 24: confirmed by UPDATED or UNCHANGED, or after a lost reply by reading the claim back."""
        try:
            reply = self._trader.update_claim(request_id, final)
            confirmed = reply["status"] in ("UPDATED", "UNCHANGED")
            if not confirmed:
                logger.error("claim %s: the trader refused %s (%s); the case stays withheld", request_id, final,
                             reply.get("code"))
        except TraderUnavailable:
            confirmed = self._claim_reads(request_id, final)
        if confirmed:
            self._store.confirm_report(request_id, now=self._now())
        else:
            logger.warning("claim %s: %s not confirmed by the trader yet; sent again next tick", request_id, final)
        return confirmed

    def _claim_reads(self, request_id: str, state: str) -> bool:
        try:
            claim = self._trader.claim_readback(request_id)
        except TraderUnavailable:
            return False
        return claim is not None and claim["state"] == state
```

`schema.py` `apply_research_migrations` gains, last:

```python
    from trader.research.service_store import apply_service_migrations
    apply_service_migrations(migrator)
```

- [ ] **Step 5: Run** `.venv/bin/python -m pytest tests/research/test_evaluation_service.py tests/research -q` — expected: PASS.

- [ ] **Step 6: Commit** `feat: claim at the trader before any research evaluation runs`

```
feat: claim at the trader before any research evaluation runs

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
```

### Task 7: `attest_from_judgment` (the review handoff, paper only)

**Files:** Create `trader/research/judgment_attest.py`, `tests/research/test_judgment_attest.py`. Modify `trader/research/review.py` (export `REVIEW_NARRATIVE_FIELDS`).

**Interfaces.**
- Consumes Plan 1's `get_backtest_judgment` view (`verdict`, `kind`, `case_digest`, `body.jev_model`, `body.decided_at`, `body.narrative`, `binding`), `load_verified_case`, `CaseRefused`; Task 6's `ResearchStore.case` and `TraderPort.judgment`; the unchanged `attest_and_export`.
- Produces `review.REVIEW_NARRATIVE_FIELDS = _NARRATIVE_FIELDS[3:]` (the eight §8.5 fields, the same names as Plan 1's `DeployNarrative`), `is_paper_posture(environ, trader_config) -> bool`, `JudgmentAttest(*, research_db, store, trader, signer, artifacts_root, repo_root, is_paper, now, ruleset=PAPER_V1)` with `attest(body, caller) -> dict`: `{"status": "ATTESTED"|"DUPLICATE"|"REFUSED", "bundle_digest", "code", "detail", "retryable", "binding"}`. `binding` = `{strategy_path, class_name, file_hash, params, conids, bar_size, order_notional}` read back from the bundle just signed through `ArtifactVerifier` with this service's public key (the facts Plan 2's `binding_differences` compares); null on a refusal. Plan 4 builds the `register_ai_deployment` body from it only. `TRADER_UNAVAILABLE` is the one retryable refusal.

- [ ] **Step 1: Failing test** `tests/research/test_judgment_attest.py`

```python
import datetime as dt
import hashlib
import shutil
from types import SimpleNamespace

import pytest

from tests.research.evaluation_fixtures import CONIDS, evaluate_synthetic, holdout_ruleset, submit_review
from tests.research.service_fakes import AI, CLI, FakeTrader, judgment_view
from trader.data.duckdb_store import DuckDBConnection
from trader.research.eligibility import EligibilityDecisionRepository
from trader.research.evaluation_case import CASE_DOMAIN, EvaluationCase, write_evaluation_case
from trader.research.judgment_attest import JudgmentAttest, is_paper_posture
from trader.research.review import REVIEW_NARRATIVE_FIELDS
from trader.research.service_store import ResearchStore
from trader.research.signing import AttestationSigner

NOW = dt.datetime(2026, 10, 8, 21, tzinfo=dt.timezone.utc)
NARRATIVE = {name: f"jev on {name}" for name in REVIEW_NARRATIVE_FIELDS}
KEY = "strategies/time_of_day.py:TimeOfDay"

pytestmark = pytest.mark.timeout(240)


@pytest.fixture(scope="module")
def evaluated(tmp_path_factory):
    repo = tmp_path_factory.mktemp("evaluated")
    evaluation = evaluate_synthetic(repo, str(repo / "bars.duckdb"))
    assert evaluation.result.stage == "complete"
    return repo, evaluation.result, evaluation.spec


def make_case(result, spec, db, file_hash, **changes) -> EvaluationCase:
    decision = EligibilityDecisionRepository(db).get(result.decision_digest)
    raw = {"schema_version": CASE_DOMAIN, "kind": "INITIAL", "request_id": "sha256:" + "1" * 64,
           "claim_day": "2026-10-08", "strategy_key": KEY, "strategy_file_hash": file_hash,
           "cohort": [dict(spec.params)], "conids": sorted(CONIDS), "bar_size": "15 mins", "stage": "COMPLETE",
           "holdout_passed": True, "selected_params": dict(spec.params), "family_id": result.family_id,
           "selected_trial_id": "t1",
           "artifact_id": result.artifact_id, "eligibility_decision_digest": result.decision_digest,
           "decision_state": decision.state, "ruleset_digest": decision.ruleset_digest,
           "final_rule_results": [{"code": r.code, "passed": r.passed} for r in decision.results],
           "renewal": None, "created_at": NOW.isoformat(),
           "evidence": {"holdout": {"start": "x", "end": "y", "passed": True, "detail": ""}}}
    raw.update(changes)
    return EvaluationCase.model_validate(raw)


@pytest.fixture
def world(evaluated, tmp_path):
    source, result, spec = evaluated
    repo = tmp_path / "repo"
    shutil.copytree(source, repo)
    db = DuckDBConnection.get_instance(str(repo / "research.duckdb"))
    store, trader, signer = ResearchStore(db), FakeTrader(), AttestationSigner.generate()
    file_hash = "sha256:" + hashlib.sha256((repo / "strategies" / "time_of_day.py").read_bytes()).hexdigest()

    def sign(**changes):
        case = make_case(result, spec, db, file_hash, **changes)
        digest = write_evaluation_case(repo / "artifacts" / "cases", case, signer)
        store.record_case(digest, case.request_id, case.stage, NOW)
        return digest, case

    digest, case = sign()
    paper = {"value": True}
    attest = JudgmentAttest(research_db=db, store=store, trader=trader, signer=signer,
                            artifacts_root=repo / "artifacts", repo_root=repo, is_paper=lambda: paper["value"],
                            now=lambda: NOW, ruleset=holdout_ruleset())

    def judge(verdict="DEPLOY", narrative=NARRATIVE, on=None, binding=None):
        on_digest, on_case = on or (digest, case)
        trader.judgments["jdg-00000001"] = judgment_view(
            "jdg-00000001", on_digest, verdict, decided_at="2026-10-08T20:30:00+00:00", narrative=narrative,
            binding=binding or {"artifact_id": on_case.artifact_id, "family_id": on_case.family_id,
                                "params": on_case.selected_params, "conids": on_case.conids,
                                "bar_size": on_case.bar_size, "strategy_file_hash": on_case.strategy_file_hash})
    return SimpleNamespace(attest=attest, judge=judge, sign=sign, db=db, paper=paper, result=result, repo=repo,
                           spec=spec, file_hash=file_hash, trader=trader)


def reviews(world):
    return world.db.execute("SELECT reviewer, reviewer_kind, holdout_opened_once_confirmed FROM operator_reviews "
                            "WHERE artifact_id = ?", [world.result.artifact_id], fetch="all")


def call(world):
    return world.attest.attest({"judgment_id": "jdg-00000001"}, AI)


def test_no_judgment_is_refused_and_writes_no_review(world):
    assert call(world)["code"] == "JUDGMENT_MISSING" and reviews(world) == []


def test_a_deploy_judgment_becomes_one_llm_review_and_one_bundle(world):
    world.judge()
    first = call(world)
    assert first["status"] == "ATTESTED" and first["bundle_digest"].startswith("sha256:")
    assert reviews(world) == [("openrouter/jev-1#jdg-00000001", "llm", True)]
    again = call(world)
    assert (again["status"], again["bundle_digest"]) == ("DUPLICATE", first["bundle_digest"])
    assert len(reviews(world)) == 1


def test_the_reply_carries_the_binding_read_from_the_signed_bundle(world):
    world.judge()
    first = call(world)
    binding = first["binding"]
    assert set(binding) == {"strategy_path", "class_name", "file_hash", "params", "conids", "bar_size",
                            "order_notional"}
    assert (binding["strategy_path"], binding["class_name"]) == ("strategies/time_of_day.py", "TimeOfDay")
    assert binding["file_hash"] == world.file_hash and binding["params"] == dict(world.spec.params)
    assert (binding["conids"], binding["bar_size"]) == (sorted(CONIDS), "15 mins")
    assert call(world)["binding"] == binding                             # a repeat answers the same binding
    world.paper.update(value=False)
    refused = call(world)
    assert (refused["status"], refused["binding"], refused["retryable"]) == ("REFUSED", None, False)


def test_an_unreachable_trader_is_a_retryable_refusal(world, monkeypatch):
    from trader.research.trader_port import TraderUnavailable

    def down(**kwargs):
        raise TraderUnavailable("no route to the trader")
    monkeypatch.setattr(world.trader, "judgment", down)
    reply = call(world)
    assert (reply["code"], reply["retryable"]) == ("TRADER_UNAVAILABLE", True) and reviews(world) == []


@pytest.mark.parametrize("setup,code", [
    (lambda w: w.judge(verdict="SHADOW"), "JUDGMENT_NOT_DEPLOY"),
    (lambda w: w.judge(verdict="NO_VERDICT"), "JUDGMENT_NOT_DEPLOY"),
    (lambda w: (w.judge(), w.paper.update(value=False)), "ACCOUNT_NOT_PAPER"),
    (lambda w: w.judge(narrative={**NARRATIVE, "episode_dominance": " "}), "NARRATIVE_INVALID"),
    (lambda w: w.judge(narrative=None), "NARRATIVE_INVALID"),
    (lambda w: w.judge(binding={"artifact_id": "other"}), "JUDGMENT_MISMATCH"),
    (lambda w: (w.judge(), (w.repo / "strategies" / "time_of_day.py").write_text("# changed\n")),
     "STRATEGY_SOURCE_CHANGED"),
])
def test_refusals_write_no_review(world, setup, code):
    setup(world)
    assert call(world)["code"] == code and reviews(world) == []


def test_a_case_this_service_did_not_sign_is_refused(world):
    world.judge(on=("sha256:" + "e" * 64, SimpleNamespace(artifact_id=None, family_id=None, selected_params=None,
                                                          conids=[], bar_size="", strategy_file_hash="")))
    assert call(world)["code"] == "CASE_UNKNOWN" and reviews(world) == []


def test_a_case_whose_binding_differs_from_the_registry_is_a_mismatch(world):
    other = world.sign(bar_size="5 mins", request_id="sha256:" + "9" * 64)
    world.judge(on=other)
    assert call(world)["code"] == "JUDGMENT_MISMATCH" and reviews(world) == []


def test_another_review_for_the_decision_is_a_conflict(world):
    world.judge()
    submit_review(world.db, world.result.artifact_id, world.result.decision_digest, kind="human")
    assert call(world)["code"] == "REVIEW_CONFLICT"


def test_only_ai_research_attests():
    attest = JudgmentAttest(research_db=None, store=None, trader=None, signer=None, artifacts_root=None,
                            repo_root=None, is_paper=lambda: True, now=lambda: NOW)
    assert attest.attest({"judgment_id": "jdg-00000001"}, CLI)["code"] == "PRINCIPAL_FORBIDDEN"


def test_paper_posture_needs_every_signal():
    assert is_paper_posture({"TRADING_MODE": "paper", "IB_ACCOUNT": "DU123"}, {"trading_mode": "paper"})
    assert not is_paper_posture({"TRADING_MODE": "paper", "IB_ACCOUNT": "U123"}, {})
    assert not is_paper_posture({"TRADING_MODE": "live", "IB_ACCOUNT": "DU123"}, {})
    assert not is_paper_posture({"TRADING_MODE": "paper", "IB_ACCOUNT": "DU123"}, {"trading_mode": "live"})
```

- [ ] **Step 2: Run** `.venv/bin/python -m pytest tests/research/test_judgment_attest.py -q` — expected: FAIL (`ImportError: REVIEW_NARRATIVE_FIELDS`).

- [ ] **Step 3: Implement.** In `review.py`, next to `NARRATIVE_FIELDS`:

```python
# The eight §8.5 narrative fields a reviewer (human or Jev) writes; the first three are linkage.
REVIEW_NARRATIVE_FIELDS = _NARRATIVE_FIELDS[3:]
```

`trader/research/judgment_attest.py`:

```python
"""attest_from_judgment (SP2c spec 5.1): a durable DEPLOY judgment becomes the paper `llm` review, then the
unchanged attest/export signs the bundle. The trader's judgment record is the authority, never the caller."""
from __future__ import annotations

import datetime as dt
import hashlib
import threading
from pathlib import Path
from typing import Any, Callable, Mapping

from trader.automation.artifact_verifier import ArtifactVerifier, ArtifactVerifierError
from trader.research.attest_export import AttestExportError, attest_and_export
from trader.research.case_builder import json_safe
from trader.research.evaluation_case import CaseRefused, load_verified_case
from trader.research.experiment_registry import ExperimentRegistry
from trader.research.review import REVIEW_NARRATIVE_FIELDS, OperatorReview, OperatorReviewRepository
from trader.research.rulesets.paper_v1 import PAPER_V1
from trader.research.strategy_key import split_strategy_key
from trader.research.trader_port import TraderUnavailable

AI_RESEARCH = "ai_research"
RETRYABLE = frozenset({"TRADER_UNAVAILABLE"})
BINDING_FIELDS = {"artifact_id": "artifact_id", "family_id": "family_id", "params": "selected_params",
                  "conids": "conids", "bar_size": "bar_size", "strategy_file_hash": "strategy_file_hash"}


def is_paper_posture(environ: Mapping[str, str], trader_config: Mapping[str, Any]) -> bool:
    """Ruling 12: every posture signal must say paper."""
    configured = trader_config.get("trading_mode")
    return (environ.get("TRADING_MODE") == "paper" and configured in (None, "", "paper")
            and str(environ.get("IB_ACCOUNT", "")).startswith("DU"))


class _Refused(Exception):
    def __init__(self, code: str, detail: str):
        super().__init__(f"{code}: {detail}")
        self.code, self.detail = code, detail


def _reply(status: str, bundle_digest=None, code=None, detail=None, binding=None) -> dict:
    return {"status": status, "bundle_digest": bundle_digest, "code": code, "detail": detail,
            "retryable": code in RETRYABLE, "binding": binding}


def _decided_at(raw: Any) -> dt.datetime:
    try:
        moment = dt.datetime.fromisoformat(str(raw))
    except ValueError:
        raise _Refused("JUDGMENT_INVALID", "decided_at is not ISO-8601") from None
    if moment.tzinfo is None:
        raise _Refused("JUDGMENT_INVALID", "decided_at has no offset")
    return moment


class JudgmentAttest:
    def __init__(self, *, research_db: Any, store: Any, trader: Any, signer: Any, artifacts_root: Path,
                 repo_root: Path, is_paper: Callable[[], bool], now: Callable[[], dt.datetime], ruleset=PAPER_V1):
        self._db, self._store, self._trader, self._signer = research_db, store, trader, signer
        self._artifacts_root, self._repo_root = artifacts_root, repo_root
        self._is_paper, self._now, self._ruleset = is_paper, now, ruleset
        self._lock = threading.Lock()

    def attest(self, body: Mapping[str, Any], caller: Any) -> dict:
        if caller.principal != AI_RESEARCH:
            return _reply("REFUSED", code="PRINCIPAL_FORBIDDEN", detail="ai_research only")
        try:
            with self._lock:
                status, bundle, binding = self._attest(str(body["judgment_id"]))
        except _Refused as refused:
            return _reply("REFUSED", code=refused.code, detail=refused.detail)
        return _reply(status, "sha256:" + bundle.name.removeprefix("sha256_"), binding=binding)

    def _attest(self, judgment_id: str) -> tuple[str, Path, dict]:
        if not self._is_paper():
            raise _Refused("ACCOUNT_NOT_PAPER", "attestation from a judgment is paper only")
        try:
            judgment = self._trader.judgment(judgment_id=judgment_id)
        except TraderUnavailable as exc:
            raise _Refused("TRADER_UNAVAILABLE", str(exc)) from None
        if judgment is None:
            raise _Refused("JUDGMENT_MISSING", f"the trader has no judgment {judgment_id}")
        if judgment["verdict"] != "DEPLOY" or judgment["kind"] != "INITIAL":
            raise _Refused("JUDGMENT_NOT_DEPLOY", f"judgment {judgment_id} is {judgment['kind']} {judgment['verdict']}")
        case = self._own_case(judgment["case_digest"])
        artifact = self._check_binding(case, judgment)
        review = self._review(case, judgment, artifact)
        status = self._record_review(review)
        try:
            bundle = attest_and_export(self._db, artifact_id=artifact.artifact_id, signer=self._signer,
                                       artifacts_root=self._artifacts_root, now=self._now(), ruleset=self._ruleset)
        except AttestExportError as exc:
            raise _Refused("ATTEST_FAILED", str(exc)) from None
        return status, bundle, self._bundle_binding(bundle, artifact.artifact_id)

    def _bundle_binding(self, bundle: Path, artifact_id: str) -> dict:
        """The facts Plan 2's binding_differences compares, read back from the bundle this service just signed."""
        try:
            verified = ArtifactVerifier([self._signer.public_key]).verify(bundle, "paper", artifact_id, self._now())
        except (ArtifactVerifierError, OSError, ValueError) as exc:
            raise _Refused("ATTEST_FAILED", f"the signed bundle does not verify: {type(exc).__name__}") from None
        attested = verified.attested_strategy
        if attested is None or attested.bar_size is None or attested.order_notional is None:
            raise _Refused("ATTEST_FAILED", "the signed bundle lacks the strategy, bar size or order notional")
        return {"strategy_path": attested.strategy_path, "class_name": attested.class_name,
                "file_hash": "sha256:" + attested.source_digest, "params": json_safe(dict(verified.parameters)),
                "conids": sorted(int(conid) for conid in verified.allowlist), "bar_size": attested.bar_size,
                "order_notional": attested.order_notional}

    def _own_case(self, digest: str) -> Any:
        """The judgment must name a case this service signed; the file must still verify and qualify."""
        if self._store.case(case_digest=digest) is None:
            raise _Refused("CASE_UNKNOWN", f"case {digest} was not signed by this research service")
        try:
            case = load_verified_case(Path(self._artifacts_root) / "cases", digest,
                                      {self._signer.public_key_id: self._signer.public_key})
        except CaseRefused as refused:
            raise _Refused(refused.code, refused.detail) from None
        holdout = case.evidence.get("holdout") or {}
        qualifies = (case.kind == "INITIAL" and case.stage == "COMPLETE" and case.decision_state == "PAPER_ELIGIBLE"
                     and case.ruleset_digest == self._ruleset.digest and case.holdout_passed is True
                     and holdout.get("passed") is True
                     and case.final_rule_results and all(r.passed for r in case.final_rule_results))
        if not qualifies:
            raise _Refused("CASE_NOT_DEPLOYABLE", f"case stage {case.stage} cannot lead to a bundle")
        return case

    def _check_binding(self, case: Any, judgment: Mapping[str, Any]) -> Any:
        """The trader's binding, the case header, the registry and the file on disk name one candidate."""
        binding = judgment.get("binding") or {}
        if any(binding.get(key) != getattr(case, attr) for key, attr in BINDING_FIELDS.items()):
            raise _Refused("JUDGMENT_MISMATCH", "the judgment's binding and the case differ")
        registry = ExperimentRegistry(self._db)
        artifact = registry.get_artifact(case.artifact_id)
        family = None if artifact is None else registry.get_family(artifact.family_id)
        if artifact is None or family is None:
            raise _Refused("JUDGMENT_MISMATCH", "the case names an artifact the registry does not have")
        path, class_name = split_strategy_key(case.strategy_key)
        protocol = family.validation_protocol
        bound = (artifact.family_id == case.family_id
                 and dict(artifact.selected_parameters) == dict(case.selected_params)
                 and (family.strategy_path, family.class_name) == (path, class_name)
                 and "sha256:" + family.source_tree_digest == case.strategy_file_hash
                 and sorted(protocol["conids"]) == list(case.conids) and protocol["bar_size"] == case.bar_size
                 and artifact.holdout_opened is True and artifact.holdout_passed is True)
        if not bound:
            raise _Refused("JUDGMENT_MISMATCH", "the case and the registry name different bindings")
        current = (Path(self._repo_root) / path).read_bytes()
        if "sha256:" + hashlib.sha256(current).hexdigest() != case.strategy_file_hash:
            raise _Refused("STRATEGY_SOURCE_CHANGED", "the strategy file changed after the evaluation")
        return artifact

    def _review(self, case: Any, judgment: Mapping[str, Any], artifact: Any) -> OperatorReview:
        body = judgment["body"]
        narrative = body.get("narrative") or {}
        if set(narrative) != set(REVIEW_NARRATIVE_FIELDS):
            raise _Refused("NARRATIVE_INVALID", "the judgment does not carry exactly the §8.5 narrative fields")
        try:
            return OperatorReview(
                artifact_id=artifact.artifact_id, eligibility_decision_digest=case.eligibility_decision_digest,
                reviewer=f"{body['jev_model']}#{judgment['judgment_id']}",
                reviewed_at=_decided_at(body["decided_at"]),
                holdout_opened_once_confirmed=artifact.holdout_opened,   # set by code from the registry
                reviewer_kind="llm", **{name: narrative[name] for name in REVIEW_NARRATIVE_FIELDS})
        except ValueError as exc:
            raise _Refused("NARRATIVE_INVALID", str(exc)) from None

    def _record_review(self, review: OperatorReview) -> str:
        existing = {row[0] for row in self._db.execute(
            "SELECT review_digest FROM operator_reviews WHERE artifact_id = ? AND eligibility_decision_digest = ?",
            [review.artifact_id, review.eligibility_decision_digest], fetch="all")}
        if existing - {review.digest}:
            raise _Refused("REVIEW_CONFLICT", "another review already exists for this decision")
        if existing:
            return "DUPLICATE"
        OperatorReviewRepository(self._db).record(review)
        return "ATTESTED"
```

(`attest_and_export` also refuses unless exactly one review exists for the decision, so the two checks agree.)

- [ ] **Step 4: Run** `.venv/bin/python -m pytest tests/research/test_judgment_attest.py tests/research/test_attest_export.py -q` — expected: PASS.

- [ ] **Step 5: Commit** `feat: turn a durable deploy judgment into the paper llm review and a bundle`

```
feat: turn a durable deploy judgment into the paper llm review and a bundle

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
```

### Task 8: The research RPC surface and the process entry

**Files:** Create `trader/research/research_surface.py`, `trader/research_service.py`, `tests/research/test_research_surface.py`.

**Interfaces.**
- Produces wire models `SubmitEvaluationRequest` (`kind` INITIAL or RENEWAL, Cross-plan shape), `GetEvaluationRequest`, `AttestFromJudgmentRequest`, and `build_research_registry(*, evaluations, attest) -> TypedRpcRegistry` (one registry, both roles).
- Produces `trader.research_service.build_runtime(*, config_path, environ, now) -> ResearchRuntime` and `main()`. Ports: query `research_typed_query_port` (default 42106), command `research_typed_command_port` (default 42107) from `trader.yaml`; bind address `RESEARCH_TYPED_BIND_ADDRESS` (default `tcp://127.0.0.1`).
- Consumes Plan 1's `load_backtest_judge_config`, `default_cases_dir` (via `artifacts_root/cases`).

- [ ] **Step 1: Failing test** `tests/research/test_research_surface.py`

```python
import pytest

from tests.rpc_identity_fixtures import ServedStack, make_identities, write_keyset
from trader.messaging.principals import KNOWN_PRINCIPALS, RESEARCH_ACL
from trader.research.research_surface import build_research_registry

BODIES = {
    "submit_evaluation": {"kind": "INITIAL", "strategy_key": "strategies/x.py:X", "cohort": [{"A": 1}],
                          "conids": [1, 2], "bar_size": "15 mins"},
    "get_evaluation": {"request_id": "sha256:" + "a" * 64},
    "attest_from_judgment": {"judgment_id": "jdg-00000001"},
}


class Recorder:
    def __init__(self):
        self.calls = []

    def submit(self, body, caller):
        self.calls.append(("submit", caller.principal))
        return {"status": "ACCEPTED"}

    def get(self, request_id, caller):
        self.calls.append(("get", caller.principal))
        return {"found": False}

    def attest(self, body, caller):
        self.calls.append(("attest", caller.principal))
        return {"status": "REFUSED"}


@pytest.fixture
def stack():
    recorder = Recorder()
    registry = build_research_registry(evaluations=recorder, attest=recorder)
    served = ServedStack({("research", "command"): registry, ("research", "query"): registry}, make_identities())
    served.recorder = recorder
    return served


@pytest.mark.parametrize("role,method", sorted(RESEARCH_ACL))
def test_each_method_is_refused_at_the_server_for_every_caller_outside_its_row(stack, role, method):
    for caller in sorted(KNOWN_PRINCIPALS - RESEARCH_ACL[(role, method)]):
        code = stack.raw_code(stack.signed(caller, server="research", role=role, method=method,
                                           body=BODIES[method]))
        assert code in ("PERMISSION_DENIED", "AUTHENTICATION_ERROR"), (caller, code)
    assert stack.recorder.calls == []
    for caller in sorted(RESEARCH_ACL[(role, method)]):
        assert stack.raw_code(stack.signed(caller, server="research", role=role, method=method,
                                           body=BODIES[method])) == "OK"


@pytest.mark.parametrize("method,body", [
    ("submit_evaluation", {**BODIES["submit_evaluation"], "conids": [1, True]}),
    ("submit_evaluation", {**BODIES["submit_evaluation"], "research_day": "2024-03-29"}),
    ("submit_evaluation", {"kind": "RENEWAL", "strategy_key": "strategies/x.py:X"}),
    ("attest_from_judgment", {"judgment_id": "jdg-00000001", "verdict": "DEPLOY"}),
])
def test_wire_models_are_strict(stack, method, body):
    assert stack.raw_code(stack.signed("ai_research", server="research", role="command", method=method,
                                       body=body)) != "OK"


def test_a_renewal_shape_reaches_the_service_which_refuses_it(stack):
    body = {"kind": "RENEWAL", "prior_version_digest": "sha256:" + "9" * 64}
    assert stack.raw_code(stack.signed("ai_research", server="research", role="command",
                                       method="submit_evaluation", body=body)) == "OK"
    assert stack.recorder.calls == [("submit", "ai_research")]


def test_the_signing_key_must_not_be_an_rpc_key(tmp_path, monkeypatch):
    from trader.research.signing import InvalidKeyType, load_signing_key
    write_keyset(tmp_path / "rpc", ["research"])
    monkeypatch.setenv("MMR_RPC_KEYS_DIR", str(tmp_path / "rpc"))
    with pytest.raises(InvalidKeyType):
        load_signing_key(str(tmp_path / "rpc" / "research.key"))
```

- [ ] **Step 2: Run** `.venv/bin/python -m pytest tests/research/test_research_surface.py -q` — expected: FAIL (`ModuleNotFoundError: trader.research.research_surface`).

- [ ] **Step 3: Implement** `trader/research/research_surface.py`

```python
"""The research server's typed RPC methods (SP2c spec 5.1). Every handler re-checks its caller."""
from __future__ import annotations

from typing import Any, Literal, Optional, Union

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictFloat, StrictInt, StrictStr, model_validator

from trader.messaging.principals import RESEARCH_ACL
from trader.messaging.typed_rpc import TypedRpcRegistry

Scalar = Union[StrictBool, StrictInt, StrictFloat, StrictStr]
INITIAL_FIELDS = ("strategy_key", "cohort", "conids", "bar_size")


class SubmitEvaluationRequest(BaseModel):
    """Plan 4's shape: an INITIAL candidate, or a RENEWAL (refused until SP2c Plan 5, ruling 17)."""
    model_config = ConfigDict(extra="forbid", strict=True)
    kind: Literal["INITIAL", "RENEWAL"]
    strategy_key: Optional[str] = Field(default=None, max_length=200)
    cohort: Optional[list[dict[str, Scalar]]] = Field(default=None, min_length=1, max_length=10)
    conids: Optional[list[StrictInt]] = Field(default=None, min_length=1, max_length=50)
    bar_size: Optional[str] = Field(default=None, max_length=16)
    prior_version_digest: Optional[str] = Field(default=None, pattern=r"^sha256:[0-9a-f]{64}$")

    @model_validator(mode="after")
    def _one_shape(self) -> "SubmitEvaluationRequest":
        initial = [getattr(self, name) is not None for name in INITIAL_FIELDS]
        if self.kind == "INITIAL" and (not all(initial) or self.prior_version_digest is not None):
            raise ValueError("an INITIAL request names strategy_key, cohort, conids and bar_size only")
        if self.kind == "RENEWAL" and (any(initial) or self.prior_version_digest is None):
            raise ValueError("a RENEWAL request names prior_version_digest only")
        return self

    def to_service(self) -> dict:
        return self.model_dump(exclude_none=True)


class GetEvaluationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    request_id: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")


class AttestFromJudgmentRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    judgment_id: str = Field(pattern=r"^[A-Za-z0-9_-]{8,96}$")          # Plan 1's judgment id rule


def build_research_registry(*, evaluations: Any, attest: Any) -> TypedRpcRegistry:
    registry = TypedRpcRegistry(acl=RESEARCH_ACL)
    registry.register("command", "submit_evaluation", SubmitEvaluationRequest, dict,
                      lambda request, caller: evaluations.submit(request.to_service(), caller),
                      execution="thread", with_caller=True)
    registry.register("query", "get_evaluation", GetEvaluationRequest, dict,
                      lambda request, caller: evaluations.get(request.request_id, caller),
                      execution="thread", with_caller=True)
    registry.register("command", "attest_from_judgment", AttestFromJudgmentRequest, dict,
                      lambda request, caller: attest.attest(request.model_dump(), caller),
                      execution="thread", with_caller=True)
    return registry
```

`trader/research_service.py`

```python
"""The research service: `python -m trader.research_service` (SP2c spec 5.1).

A typed RPC server (42106 query, 42107 command) that claims evaluation slots at the trader, runs cohort
evaluations one at a time, signs evaluation cases, turns a durable DEPLOY judgment into the paper llm review
and a bundle, and replays judged strategies each night. It talks to no broker and calls no model.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import logging
import os
import signal
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping

import yaml

from trader.automation.backtest_judge_config import load_backtest_judge_config
from trader.data.duckdb_store import DuckDBConnection
from trader.data.schema_migrations import SchemaMigrator
from trader.data.universe import UniverseAccessor
from trader.messaging.typed_rpc import ServiceIdentity, TypedRpcClient, TypedRpcServer
from trader.research.cohort import build_cohort_spec
from trader.research.cohort_evaluation import evaluate_cohort
from trader.research.evaluation import EvaluationPaths
from trader.research.evaluation_service import EvaluationService
from trader.research.experiment_registry import ExperimentRegistry
from trader.research.judgment_attest import JudgmentAttest, is_paper_posture
from trader.research.research_surface import build_research_registry
from trader.research.schema import apply_research_migrations
from trader.research.service_config import load_research_service_config
from trader.research.service_store import ResearchStore
from trader.research.signing import AttestationSigner
from trader.research.strategy_paths import repo_root
from trader.research.trader_port import TraderPort, TraderUnavailable
from trader.simulation.execution_costs import load_execution_costs_config

logger = logging.getLogger("trader.research_service")
CONFIG_DIR = Path("~/.config/mmr").expanduser()
ARTIFACTS_ROOT = Path("~/.local/share/mmr/artifacts").expanduser()
DEFAULT_RESEARCH_DB = "~/.local/share/mmr/data/mmr_research.duckdb"
RECOVER_RETRY_SECONDS = 10.0


@dataclass
class ResearchRuntime:
    evaluations: EvaluationService
    servers: list
    background: list            # callables(stop) run on their own threads (worker, shadow scheduler)


def _path(raw: Mapping[str, Any], key: str) -> str:
    value = raw.get(key)
    if not value:
        raise ValueError(f"trader.yaml: {key} is required by the research service")
    return str(Path(value).expanduser())


def build_runtime(*, config_path: str, environ: Mapping[str, str], now: Callable[[], dt.datetime]) -> ResearchRuntime:
    raw = yaml.safe_load(Path(config_path).read_text()) or {}
    judge = load_backtest_judge_config((raw.get("ai_paper") or {}).get("backtest_judge"))
    config = load_research_service_config(raw)
    identity = ServiceIdentity.load("research")
    signer = AttestationSigner.from_key_file(str(CONFIG_DIR / "keys" / "private" / "signing.pem"))
    db = DuckDBConnection.get_instance(str(Path(environ.get("MMR_RESEARCH_DUCKDB") or DEFAULT_RESEARCH_DB).expanduser()))
    apply_research_migrations(SchemaMigrator(db))
    trader_address = environ.get("TRADER_TYPED_ADDRESS", "tcp://127.0.0.1")
    query = TypedRpcClient("query", identity, server="trader", address=trader_address,
                           port=int(raw.get("typed_query_port", 42101)))
    command = TypedRpcClient("command", identity, server="trader", address=trader_address,
                             port=int(raw.get("typed_command_port", 42102)))
    query.connect()
    command.connect()
    trader = TraderPort(query, command)
    duckdb_path, history_path = _path(raw, "duckdb_path"), _path(raw, "history_duckdb_path")
    universe_library = raw.get("universe_library", "Universes")
    universe = UniverseAccessor(duckdb_path, universe_library)
    costs_path = CONFIG_DIR / "execution_costs.yaml"
    costs = load_execution_costs_config(str(costs_path))
    root = repo_root()
    paths = EvaluationPaths(history_db=history_path, universe_db=duckdb_path, universe_library=universe_library,
                            execution_costs=str(costs_path), repo_root=root,
                            reports_dir=ARTIFACTS_ROOT / "reports", summaries_dir=ARTIFACTS_ROOT / "evaluations")
    registry, store = ExperimentRegistry(db), ResearchStore(db)

    def build_spec(body):
        return build_cohort_spec(body, config=config, judge=judge, universe_accessor=universe, costs_config=costs,
                                 repo_root=root, registry=registry)

    evaluations = EvaluationService(
        store=store, trader=trader, build_spec=build_spec,
        evaluate=lambda spec: evaluate_cohort(spec, research_db=db, paths=paths, now=now), signer=signer,
        artifacts_root=ARTIFACTS_ROOT, warmup_sessions=judge.shadow_warmup_sessions,
        order_notional=config.order_notional, queue_max=config.queue_max, now=now)
    attest = JudgmentAttest(research_db=db, store=store, trader=trader, signer=signer, artifacts_root=ARTIFACTS_ROOT,
                            repo_root=root, is_paper=lambda: is_paper_posture(environ, raw), now=now)
    rpc = build_research_registry(evaluations=evaluations, attest=attest)
    bind = environ.get("RESEARCH_TYPED_BIND_ADDRESS", "tcp://127.0.0.1")
    servers = [TypedRpcServer("query", rpc, identity, address=bind,
                              port=int(raw.get("research_typed_query_port", 42106))),
               TypedRpcServer("command", rpc, identity, address=bind,
                              port=int(raw.get("research_typed_command_port", 42107)))]
    return ResearchRuntime(evaluations=evaluations, servers=servers, background=[evaluations.serve_forever])


def _recover_until_reachable(evaluations: EvaluationService, stop: threading.Event) -> None:
    while not stop.is_set():
        try:
            evaluations.recover()
            return
        except TraderUnavailable as exc:
            logger.warning("trader unreachable during recovery (%s); retrying", exc)
            stop.wait(RECOVER_RETRY_SECONDS)


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    now = lambda: dt.datetime.now(dt.timezone.utc)                    # noqa: E731
    runtime = build_runtime(config_path=os.environ.get("TRADER_CONFIG", str(CONFIG_DIR / "trader.yaml")),
                            environ=os.environ, now=now)
    stop = threading.Event()
    _recover_until_reachable(runtime.evaluations, stop)
    for job in runtime.background:
        threading.Thread(target=job, args=(stop,), daemon=True).start()
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, lambda: (stop.set(), loop.stop()))
    loop.run_until_complete(asyncio.gather(*(server.serve() for server in runtime.servers)))
    logger.info("research service serving on 42106/42107")
    loop.run_forever()


if __name__ == "__main__":
    main()
```

Remove the `pytest.importorskip` line from `tests/test_compose_research_service.py::test_research_service_never_names_the_journal` now that the module exists.

- [ ] **Step 4: Run** `.venv/bin/python -m pytest tests/research/test_research_surface.py tests/test_compose_research_service.py tests/test_research_principal.py -q` — expected: PASS.

- [ ] **Step 5: Commit** `feat: serve the research methods over signed typed rpc`

```
feat: serve the research methods over signed typed rpc

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
```

### Task 9: Backtester `trading_start` (warm-up without trading)

**Files:** Modify `trader/simulation/backtester.py`, `trader/research/evaluation_jobs.py`. Create `tests/test_backtester_trading_start.py`.

**Interfaces.** Produces `BacktestConfig.trading_start: Optional[dt.datetime] = None` (timezone-aware or refused) and `WindowJob.trading_start: Optional[dt.datetime] = None` (passed through by `run_window_job`).

- [ ] **Step 1: Failing test** `tests/test_backtester_trading_start.py`

```python
import datetime as dt

import pandas as pd
import pytest

from tests.research.evaluation_fixtures import CONIDS, TIME_OF_DAY_STRATEGY, write_trend_bars, write_universe
from trader.data.data_access import TickStorage
from trader.data.universe import UniverseAccessor
from trader.objects import BarSize
from trader.simulation.backtester import Backtester, BacktestConfig

UTC = dt.timezone.utc
START = dt.datetime(2024, 3, 18, tzinfo=UTC)
TRADING_START = dt.datetime(2024, 3, 20, tzinfo=UTC)
END = dt.datetime(2024, 3, 22, 23, 59, tzinfo=UTC)


@pytest.fixture
def run(tmp_path, tmp_duckdb_path):
    write_universe(tmp_duckdb_path)
    write_trend_bars(tmp_duckdb_path, drift=0.0006)
    strategy = tmp_path / "time_of_day.py"
    strategy.write_text(TIME_OF_DAY_STRATEGY)

    def _run(trading_start, params):
        config = BacktestConfig(start_date=START, end_date=END, initial_capital=100_000.0,
                                bar_size=BarSize.parse_str("15 mins"), order_notional=1900.0,
                                trading_start=trading_start)
        return Backtester(TickStorage(tmp_duckdb_path), config).run_from_module(
            str(strategy), "TimeOfDay", CONIDS[:2], universe_accessor=UniverseAccessor(tmp_duckdb_path, "Universes"),
            params=params)
    return _run


def _utc(ts) -> pd.Timestamp:
    ts = pd.Timestamp(ts)
    return ts.tz_localize("UTC") if ts.tzinfo is None else ts


def test_warm_up_books_no_fill_no_cost_and_equity_starts_at_initial_capital(run):
    result = run(TRADING_START, {"ENTRY_MINUTE": 600, "EXIT_MINUTE": 660})
    assert result.trades and all(_utc(t.timestamp) >= pd.Timestamp(TRADING_START) for t in result.trades)
    assert _utc(result.equity_curve.index[0]) >= pd.Timestamp(TRADING_START)
    assert result.equity_curve.iloc[0] == 100_000.0


def test_a_signal_on_the_last_warm_up_bar_never_fills_at_the_first_trading_open(run):
    result = run(TRADING_START, {"ENTRY_MINUTE": 945, "EXIT_MINUTE": 600})   # BUY on every 15:45 bar
    first_day = {_utc(t.timestamp).tz_convert("America/New_York").date() for t in result.trades}
    assert dt.date(2024, 3, 20) not in first_day                              # 03-19 15:45 BUY was dropped


def test_without_trading_start_the_warm_up_days_trade(run):
    days = {_utc(t.timestamp).tz_convert("America/New_York").date()
            for t in run(None, {"ENTRY_MINUTE": 600, "EXIT_MINUTE": 660}).trades}
    assert dt.date(2024, 3, 18) in days


def test_a_naive_trading_start_is_refused():
    with pytest.raises(ValueError):
        BacktestConfig(trading_start=dt.datetime(2024, 3, 20))
```

- [ ] **Step 2: Run** `.venv/bin/python -m pytest tests/test_backtester_trading_start.py -q` — expected: FAIL (`TypeError: unexpected keyword argument 'trading_start'`).

- [ ] **Step 3: Implement.** In `BacktestConfig` add, after `fill_policy`:

```python
    # Warm-up (SP2c spec 7): bars before trading_start only feed the strategy's state. Their signals
    # are dropped, nothing fills, no cost is charged, and equity starts at initial_capital here.
    trading_start: Optional[dt.datetime] = None

    def __post_init__(self) -> None:
        if self.trading_start is not None and (self.trading_start.tzinfo is None
                                               or self.trading_start.utcoffset() is None):
            raise ValueError("BacktestConfig.trading_start must be timezone-aware")
```

In `run`, before the bar loop:

```python
        trading_start = None if self.config.trading_start is None else pd.Timestamp(self.config.trading_start)
        warmup_signals_dropped = 0
```

At the top of the loop body, after `bars_this_ts` is built:

```python
            stamp = pd.Timestamp(timestamp)
            warming = trading_start is not None and (
                stamp.tz_localize('UTC') if stamp.tzinfo is None else stamp) < trading_start
```

Then: step 0 becomes `if live_rules is not None and not warming:`; step 1 becomes `if not warming and self.config.fill_policy == 'next_open' and pending_signals:`; in step 3 replace `if not signal: continue` with

```python
                if not signal:
                    continue
                if warming:
                    warmup_signals_dropped += 1     # a warm-up signal never fills, not even at the first open
                    continue
```

and before step 4 add `if warming: continue`. Log `warmup_signals_dropped` once after the loop at INFO. Steps 2 and 2b are unchanged (no position exists during warm-up, so 2b does nothing).

In `evaluation_jobs.py`, `WindowJob` gains `trading_start: Optional[dt.datetime] = None` (last field, so existing positional constructions stay valid) and `run_window_job` passes `trading_start=job.trading_start` to `BacktestConfig`.

- [ ] **Step 4: Run** `.venv/bin/python -m pytest tests/test_backtester_trading_start.py tests/research/test_evaluation_jobs.py tests/test_backtester*.py -q` — expected: PASS.

- [ ] **Step 5: Commit** `feat: let a backtest warm up without trading before trading_start`

```
feat: let a backtest warm up without trading before trading_start

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
```

### Task 10: Trader `record_shadow_result` and shadow books

**Files:** Create `trader/research/shadow_window.py`, `trader/scoreboard/shadow_ingest.py`, `trader/messaging/shadow_surface.py`, `tests/scoreboard/test_shadow_results.py`. Modify `trader/scoreboard/schema.py` (migration 120), `trader/scoreboard/store.py` (`SEALED_TABLES`), `trader/scoreboard/books.py`, `trader/scoreboard/report.py`, `trader/scoreboard/service.py`, `trader/messaging/production_api.py`, `trader/trading/command_stack.py`.

**Interfaces.**
- Consumes Plan 1's `ai_paper.judgments.get(judgment_id) -> BacktestJudgment | None` (attributes `case_digest`, `verdict`, `body["decided_at"]`; raises `JudgmentRefused`) and `ai_paper_config.backtest_judge`; Plan 2's `trader.ai_deployment_versions.version_for_judgment(judgment_id)`.
- Produces `shadow_window(decided_at, verdict, *, deploy_expiry_sessions, family_cooldown_sessions) -> (date, date)`, `RecordShadowResultRequest`, `ShadowIngest(*, store, judgments, versions, config, now)` with `record(request, caller) -> dict`, `register_shadow_surface(registry, ingest)`, `build_shadow_books(rows) -> list[dict]`, report key `shadow_books`, `ReportInputs.shadow_rows`.

- [ ] **Step 1: Failing test** `tests/scoreboard/test_shadow_results.py`

```python
import datetime as dt
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from tests.scoreboard.test_report import empty_inputs
from trader.research.shadow_window import shadow_window
from trader.scoreboard.books import build_shadow_books
from trader.scoreboard.report import build_report
from trader.scoreboard.shadow_ingest import RecordShadowResultRequest, ShadowIngest

RESEARCH, CLI = SimpleNamespace(principal="research"), SimpleNamespace(principal="cli")
CASE = "sha256:" + "c" * 64
DECIDED = "2024-03-28T21:00:00+00:00"                     # after the 2024-03-28 close
CONFIG = SimpleNamespace(deploy_expiry_sessions=20, family_cooldown_sessions=10)


def body(**overrides):
    values = {"judgment_id": "j1", "case_digest": CASE, "verdict": "DEPLOY", "session_date": "2024-04-01",
              "status": "COMPLETE", "reason": None, "pnl_usd": 12.5, "fees_usd": 1.0, "trades": 2,
              "end_equity_usd": 100_012.5, "bar_size": "15 mins"}
    values.update(overrides)
    return RecordShadowResultRequest(**values)


@pytest.fixture
def ingest(store):
    judgments = {"j1": SimpleNamespace(judgment_id="j1", case_digest=CASE, verdict="DEPLOY", kind="INITIAL",
                                       body={"decided_at": DECIDED})}
    versions = {"j1": None}
    service = ShadowIngest(store=store, judgments=SimpleNamespace(get=judgments.get),
                           versions=SimpleNamespace(version_for_judgment=versions.get), config=CONFIG,
                           now=lambda: dt.datetime(2024, 4, 1, 22, tzinfo=dt.timezone.utc))
    service.versions_map = versions
    return service


def test_window_is_fixed_at_the_decision():
    assert shadow_window(dt.datetime.fromisoformat(DECIDED), "DEPLOY", deploy_expiry_sessions=20,
                         family_cooldown_sessions=10)[0] == dt.date(2024, 4, 1)     # 03-29 is Good Friday
    first, last = shadow_window(dt.datetime.fromisoformat(DECIDED), "REJECT", deploy_expiry_sessions=20,
                                family_cooldown_sessions=10)
    assert first == dt.date(2024, 4, 1) and last == dt.date(2024, 4, 26)            # 10 + 10 sessions


def test_a_repeat_is_a_no_op_and_a_changed_row_is_refused(ingest, store):
    assert ingest.record(body(), RESEARCH)["status"] == "INSERTED"
    assert ingest.record(body(), RESEARCH)["status"] == "DUPLICATE"
    refused = ingest.record(body(pnl_usd=13.0, end_equity_usd=100_013.0), RESEARCH)
    assert (refused["status"], refused["code"]) == ("REFUSED", "CONFLICTING_DUPLICATE")
    assert len(store.fetch("shadow_results", {})) == 1 and store.verify_seals() == []


def test_a_registration_between_two_sends_does_not_make_a_second_row(ingest, store):
    ingest.record(body(), RESEARCH)
    ingest.versions_map["j1"] = "sha256:" + "v" * 64
    assert ingest.record(body(), RESEARCH)["status"] == "DUPLICATE"
    ingest.record(body(session_date="2024-04-02", end_equity_usd=100_020.0, pnl_usd=7.5), RESEARCH)
    rows = store.fetch("shadow_results", {})
    assert [r["deployment_version"] for r in rows] == [None, "sha256:" + "v" * 64]


@pytest.mark.parametrize("request_body,caller,code", [
    (body(), CLI, "PRINCIPAL_FORBIDDEN"),
    (body(judgment_id="j9"), RESEARCH, "JUDGMENT_UNKNOWN"),
    (body(verdict="SHADOW"), RESEARCH, "SHADOW_JUDGMENT_MISMATCH"),
    (body(session_date="2024-03-28"), RESEARCH, "SHADOW_SESSION_OUTSIDE_WINDOW"),
    (body(session_date="2024-04-30"), RESEARCH, "SHADOW_SESSION_OUTSIDE_WINDOW"),
])
def test_refusals(ingest, request_body, caller, code):
    assert ingest.record(request_body, caller)["code"] == code


def test_rows_are_strict():
    with pytest.raises(ValidationError):
        body(pnl_usd=None)                                         # COMPLETE needs P&L
    with pytest.raises(ValidationError):
        body(status="INCOMPLETE", reason=None, pnl_usd=None, fees_usd=None, trades=None, end_equity_usd=None)
    with pytest.raises(ValidationError):
        body(trades=True)


def test_one_book_per_verdict_never_summed_and_incomplete_shown():
    rows = [{"verdict": "DEPLOY", "judgment_id": "a", "status": "COMPLETE", "pnl_usd": 10.0, "fees_usd": 1.0,
             "trades": 2, "reason": None},
            {"verdict": "REJECT", "judgment_id": "b", "status": "COMPLETE", "pnl_usd": -5.0, "fees_usd": 1.0,
             "trades": 1, "reason": None},
            {"verdict": "REJECT", "judgment_id": "b", "status": "INCOMPLETE", "pnl_usd": None, "fees_usd": None,
             "trades": None, "reason": "BARS_MISSING"}]
    books = {b["verdict"]: b for b in build_shadow_books(rows)}
    assert set(books) == {"DEPLOY", "REJECT"}
    assert books["DEPLOY"]["pnl_usd"] == 10.0 and books["DEPLOY"]["status"] == "COMPLETE"
    assert books["REJECT"]["status"] == "INCOMPLETE" and books["REJECT"]["pnl_usd"] is None
    assert books["REJECT"]["known_pnl_usd"] == -5.0 and books["REJECT"]["incomplete_reasons"] == {"BARS_MISSING": 1}
    assert build_report(empty_inputs(shadow_rows=rows))["shadow_books"] == build_shadow_books(rows)
```

- [ ] **Step 2: Run** `.venv/bin/python -m pytest tests/scoreboard/test_shadow_results.py -q` — expected: FAIL (`ModuleNotFoundError: trader.research.shadow_window`).

- [ ] **Step 3: Implement.**

`trader/research/shadow_window.py`:

```python
"""The fixed shadow tracking window of one judgment (SP2c spec 7). Shared by research and the trader."""
from __future__ import annotations

import datetime as dt

import exchange_calendars as xcals
import pandas as pd

REJECT_EXTRA_SESSIONS = 10
TRACKED_VERDICTS = ("DEPLOY", "SHADOW", "REJECT")


def shadow_window(decided_at: dt.datetime, verdict: str, *, deploy_expiry_sessions: int,
                  family_cooldown_sessions: int) -> tuple[dt.date, dt.date]:
    """First session after the verdict, through a window fixed at the decision: DEPLOY and SHADOW for
    deploy_expiry_sessions; REJECT for its cooldown plus 10 sessions."""
    if verdict not in TRACKED_VERDICTS:
        raise ValueError(f"{verdict!r} has no shadow window")
    if decided_at.tzinfo is None or decided_at.utcoffset() is None:
        raise ValueError("decided_at must be timezone-aware")
    calendar = xcals.get_calendar("XNYS")
    first = calendar.minute_to_session(calendar.next_open(pd.Timestamp(decided_at)))
    length = deploy_expiry_sessions if verdict != "REJECT" else family_cooldown_sessions + REJECT_EXTRA_SESSIONS
    return first.date(), calendar.session_offset(first, length - 1).date()
```

`trader/scoreboard/schema.py` — add and append `(120, "sp2c_shadow_results", _SHADOW)` to `MIGRATIONS`; update the module docstring to "... SP2 Plan 2 adds 95-96; SP2c Plan 3 adds 120.":

```python
_SHADOW = (
    """CREATE TABLE IF NOT EXISTS shadow_results (
        record_id VARCHAR PRIMARY KEY, judgment_id VARCHAR NOT NULL, deployment_version VARCHAR,
        session_date DATE NOT NULL, verdict VARCHAR NOT NULL CHECK (verdict IN ('DEPLOY','SHADOW','REJECT')),
        case_digest VARCHAR NOT NULL, status VARCHAR NOT NULL CHECK (status IN ('COMPLETE','INCOMPLETE')),
        reason VARCHAR, pnl_usd DOUBLE, fees_usd DOUBLE, trades INTEGER, end_equity_usd DOUBLE,
        bar_size VARCHAR NOT NULL, body_digest VARCHAR NOT NULL, recorded_at TIMESTAMPTZ NOT NULL,
        UNIQUE (judgment_id, session_date),
        CHECK ((status = 'COMPLETE') = (pnl_usd IS NOT NULL)))""",
    "CREATE INDEX IF NOT EXISTS idx_shadow_results_version ON shadow_results(deployment_version)",
)
```

`store.py`: `SEALED_TABLES["shadow_results"] = ("record_id",)`.

`trader/scoreboard/shadow_ingest.py`:

```python
"""record_shadow_result (SP2c spec 7): one sealed forward-replay row per judgment and session, research only."""
from __future__ import annotations

import datetime as dt
from typing import Any, Callable, Literal, Optional

import exchange_calendars as xcals
from pydantic import BaseModel, ConfigDict, Field, StrictFloat, StrictInt, model_validator

from trader.automation.backtest_judgments import JudgmentRefused
from trader.research.canonical import sha256_digest
from trader.research.shadow_window import shadow_window
from trader.scoreboard.store import ScoreboardConflict, ScoreboardStore

RESEARCH = "research"
SHADOW_RESULT_DOMAIN = "mmr.shadow-result.v1"
SHADOW_BODY_DOMAIN = "mmr.shadow-result-body.v1"
_CALENDAR = xcals.get_calendar("XNYS")


class RecordShadowResultRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    judgment_id: str = Field(pattern=r"^[A-Za-z0-9_.:-]{1,128}$")
    case_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    verdict: Literal["DEPLOY", "SHADOW", "REJECT"]
    session_date: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    status: Literal["COMPLETE", "INCOMPLETE"]
    reason: Optional[str] = Field(default=None, max_length=200)
    pnl_usd: Optional[StrictFloat] = None
    fees_usd: Optional[StrictFloat] = None
    trades: Optional[StrictInt] = None
    end_equity_usd: Optional[StrictFloat] = None
    bar_size: str = Field(max_length=16)

    @model_validator(mode="after")
    def _status_matches_numbers(self) -> "RecordShadowResultRequest":
        numbers = (self.pnl_usd, self.fees_usd, self.trades, self.end_equity_usd)
        if self.status == "COMPLETE" and (any(n is None for n in numbers) or self.reason is not None):
            raise ValueError("a COMPLETE row carries pnl, fees, trades and end equity, and no reason")
        if self.status == "INCOMPLETE" and (not (self.reason or "").strip() or self.pnl_usd is not None):
            raise ValueError("an INCOMPLETE row carries a reason and no P&L")
        return self

    def digest(self) -> str:
        return "sha256:" + sha256_digest(SHADOW_BODY_DOMAIN, self.model_dump())


def _reply(status: str, record_id: Optional[str] = None, code: Optional[str] = None,
           detail: Optional[str] = None, retryable: bool = False) -> dict:
    return {"status": status, "record_id": record_id, "code": code, "detail": detail, "retryable": retryable}


class ShadowIngest:
    def __init__(self, *, store: ScoreboardStore, judgments: Any, versions: Any, config: Any,
                 now: Callable[[], dt.datetime]):
        self._store, self._judgments, self._versions, self._config, self._now = store, judgments, versions, config, now

    def record(self, request: RecordShadowResultRequest, caller: Any) -> dict:
        if caller.principal != RESEARCH:
            return _reply("REFUSED", code="PRINCIPAL_FORBIDDEN", detail="only research records shadow rows")
        try:
            judgment = self._judgments.get(request.judgment_id)
        except JudgmentRefused as refused:
            return _reply("REFUSED", code=refused.code, detail=request.judgment_id)
        if judgment is None:
            return _reply("REFUSED", code="JUDGMENT_UNKNOWN", detail=request.judgment_id, retryable=True)
        if judgment.case_digest != request.case_digest or judgment.verdict != request.verdict:
            return _reply("REFUSED", code="SHADOW_JUDGMENT_MISMATCH", detail="case or verdict differs")
        first, last = shadow_window(dt.datetime.fromisoformat(judgment.body["decided_at"]), judgment.verdict,
                                    deploy_expiry_sessions=self._config.deploy_expiry_sessions,
                                    family_cooldown_sessions=self._config.family_cooldown_sessions)
        session = dt.date.fromisoformat(request.session_date)
        if not first <= session <= last or not _CALENDAR.is_session(session):
            return _reply("REFUSED", code="SHADOW_SESSION_OUTSIDE_WINDOW", detail=f"window {first}..{last}")
        record_id = "sha256:" + sha256_digest(SHADOW_RESULT_DOMAIN, {"judgment_id": request.judgment_id,
                                                                     "session_date": request.session_date})
        version = (self._versions.version_for_judgment(request.judgment_id)
                   if request.verdict == "DEPLOY" and self._versions is not None else None)
        row = {**request.model_dump(), "record_id": record_id, "session_date": session,
               "deployment_version": version, "body_digest": request.digest(), "recorded_at": self._now()}
        try:
            return _reply(self._store.ingest_sealed_many([("shadow_results", row)]), record_id)
        except ScoreboardConflict as conflict:
            return _reply("REFUSED", record_id, "CONFLICTING_DUPLICATE", str(conflict))
```

(A repeat is recognised by `record_id` before `deployment_version` is read, so the version stored at the first insert stays.)

`books.py`:

```python
SHADOW_LABEL = "shadow"
SHADOW_BASIS = "forward replay: same backtester, cost model and live rules; never summed across verdicts"


def build_shadow_books(rows: Sequence[Mapping[str, Any]]) -> list[dict]:
    """SP2c spec 7: one book per verdict, so DEPLOY and REJECT forward results sit side by side."""
    by_verdict: dict[str, list[Mapping]] = defaultdict(list)
    for row in rows:
        by_verdict[row["verdict"]].append(row)
    books = []
    for verdict in ("DEPLOY", "SHADOW", "REJECT"):
        members = by_verdict.get(verdict)
        if not members:
            continue
        complete = [r for r in members if r["status"] == "COMPLETE"]
        incomplete = [r for r in members if r["status"] == "INCOMPLETE"]
        known = float(sum(r["pnl_usd"] for r in complete))
        books.append({
            "verdict": verdict, "label": SHADOW_LABEL, "basis": SHADOW_BASIS,
            "judgments": len({r["judgment_id"] for r in members}), "sessions": len(members),
            "complete": len(complete), "incomplete": len(incomplete),
            "status": "INCOMPLETE" if incomplete else "COMPLETE",
            "pnl_usd": None if incomplete else known, "known_pnl_usd": known if complete else None,
            "fees_usd": float(sum(r["fees_usd"] for r in complete)) if complete else None,
            "trades": sum(r["trades"] for r in complete),
            "incomplete_reasons": dict(Counter(r["reason"] for r in incomplete))})
    return books
```

`report.py`: `ReportInputs` gains a last field `shadow_rows: Sequence[Mapping[str, Any]] = ()`; `build_report` adds `"shadow_books": build_shadow_books(inputs.shadow_rows),` after `"benchmarks"`. `service.py`: `_inputs(...)` gains `shadow_rows=()` and passes it on; `report()` passes `shadow_rows=self.store.fetch("shadow_results", {})` in both branches (ruling 9: global).

`trader/messaging/shadow_surface.py`:

```python
"""record_shadow_result over typed RPC (SP2c Plan 3). research only; the handler re-checks."""
from __future__ import annotations

from typing import Any

from trader.scoreboard.shadow_ingest import RecordShadowResultRequest


def register_shadow_surface(registry: Any, ingest: Any) -> None:
    if ingest is None:
        return
    registry.register("command", "record_shadow_result", RecordShadowResultRequest, dict, ingest.record,
                      execution="thread", with_caller=True)
```

`production_api.py`, after `register_ai_ingest_surface(...)`:

```python
    from trader.messaging.shadow_surface import register_shadow_surface
    register_shadow_surface(registry, getattr(trader, 'shadow_ingest', None))
```

`command_stack.py`, after `trader.ai_ingest = scoreboard.ingest` (Plan 1 built `ai_paper.judgments`; Plan 2 set `trader.ai_deployment_versions`):

```python
    judgments = None if ai_paper is None else getattr(ai_paper, 'judgments', None)
    ai_paper_config = getattr(trader, 'ai_paper_config', None)
    trader.shadow_ingest = None if judgments is None or ai_paper_config is None else ShadowIngest(
        store=scoreboard.store, judgments=judgments, versions=getattr(trader, 'ai_deployment_versions', None),
        config=ai_paper_config.backtest_judge, now=scoreboard.now)
```

- [ ] **Step 4: Run** `.venv/bin/python -m pytest tests/scoreboard -q` — expected: PASS.

- [ ] **Step 5: Commit** `feat: record shadow replay rows at the trader and show one book per verdict`

```
feat: record shadow replay rows at the trader and show one book per verdict

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
```

### Task 11: Nightly shadow replay in the research service

**Files:** Create `trader/research/shadow_replay.py`, `tests/research/test_shadow_replay.py`. Modify `trader/research/service_store.py` (shadow methods), `trader/research_service.py` (wire the scheduler).

**Interfaces.**
- Consumes `TraderPort.judgment(case_digest=...)` (Plan 1's lookup by case digest), `TraderPort.record_shadow(body)` (Task 10 wire), Plan 1's `load_verified_case`, `shadow_window`, `run_window_job` with `WindowJob.trading_start`.
- Produces `ResearchStore.cases_without_member(since) -> list[dict]`, `add_member(...)`, `shadow_members() -> list[dict]`, `sent_sessions(judgment_id) -> set[date]`, `mark_sent(judgment_id, session, status, reply_status, now)` (`INSERTED` or `DUPLICATE` only), `mark_failed(judgment_id, session, code, detail, now)`, `failed_sessions(judgment_id) -> set[date]`, `shadow_failures() -> list[dict]`; `ShadowReplay(*, store, trader, signer, artifacts_root, paths, registry, config, judge, now, run_job=run_window_job)` with `tick() -> None`, `status() -> dict` and `serve_forever(stop)`.

- [ ] **Step 1: Failing test** `tests/research/test_shadow_replay.py`

```python
import datetime as dt
import hashlib
from types import SimpleNamespace

import pandas as pd
import pytest

from tests.research.case_fixtures import pre_holdout_result
from tests.research.evaluation_fixtures import (CONIDS, TIME_OF_DAY_STRATEGY, write_costs_config, write_trend_bars,
                                                write_universe)
from tests.research.service_fakes import FakeTrader, judgment_view
from trader.data.duckdb_store import DuckDBConnection, DuckDBDataStore
from trader.data.schema_migrations import SchemaMigrator
from trader.research import evaluation_jobs
from trader.research.case_builder import build_initial_case
from trader.research.evaluation import EvaluationPaths
from trader.research.evaluation_case import write_evaluation_case
from trader.research.evaluation_request import EvaluationRequestBody, evaluation_request_id
from trader.research.experiment_registry import ExperimentRegistry
from trader.research.schema import apply_research_migrations
from trader.research.service_config import ResearchServiceConfig
from trader.research.service_store import ResearchStore
from trader.research.shadow_replay import ShadowReplay
from trader.research.signing import AttestationSigner

UTC = dt.timezone.utc
JUDGE = SimpleNamespace(deploy_expiry_sessions=20, family_cooldown_sessions=10, shadow_warmup_sessions=5)


@pytest.fixture
def world(tmp_path, tmp_duckdb_path):
    write_universe(tmp_duckdb_path)
    write_trend_bars(tmp_duckdb_path, drift=0.0006)
    (tmp_path / "strategies").mkdir()
    (tmp_path / "strategies" / "time_of_day.py").write_text(TIME_OF_DAY_STRATEGY)
    costs = write_costs_config(tmp_path / "execution_costs.yaml")
    paths = EvaluationPaths(tmp_duckdb_path, tmp_duckdb_path, "Universes", str(costs), tmp_path,
                            tmp_path / "reports", tmp_path / "artifacts" / "evaluations")
    signer = AttestationSigner.generate()
    file_hash = "sha256:" + hashlib.sha256((tmp_path / "strategies" / "time_of_day.py").read_bytes()).hexdigest()

    def make(db_name, verdict="DEPLOY", decided_at="2024-03-15T21:00:00+00:00", bar_size="15 mins"):
        db = DuckDBConnection.get_instance(str(tmp_path / db_name))
        apply_research_migrations(SchemaMigrator(db))
        store, trader = ResearchStore(db), FakeTrader()
        params = {"ENTRY_MINUTE": 600, "EXIT_MINUTE": 660}
        body = EvaluationRequestBody.model_validate({"strategy_key": "strategies/time_of_day.py:TimeOfDay",
                                                     "cohort": [params], "conids": CONIDS, "bar_size": bar_size,
                                                     "research_day": "2024-03-15"})
        spec = SimpleNamespace(request_id=evaluation_request_id(body), body=body, strategy_key=body.strategy_key,
                               cohort=(params,), file_hash=file_hash)
        case = build_initial_case(spec, "2024-03-15", pre_holdout_result((params,)),
                                  created_at=dt.datetime(2024, 3, 15, 21, tzinfo=UTC), warmup_sessions=5)
        digest = write_evaluation_case(tmp_path / "artifacts" / "cases", case, signer)
        store.record_case(digest, case.request_id, case.stage, dt.datetime(2024, 3, 15, 21, tzinfo=UTC))
        trader.judgments["jdg-00000001"] = judgment_view("jdg-00000001", digest, verdict, decided_at=decided_at)
        clock = {"now": dt.datetime(2024, 3, 23, 12, tzinfo=UTC)}
        jobs = []

        def spy(env, job):
            jobs.append((env.bar_size, job))
            return evaluation_jobs.run_window_job(env, job)
        replay = ShadowReplay(store=store, trader=trader, signer=signer, artifacts_root=tmp_path / "artifacts",
                              paths=paths, registry=ExperimentRegistry(db), config=ResearchServiceConfig(),
                              judge=JUDGE, now=lambda: clock["now"], run_job=spy)
        return SimpleNamespace(replay=replay, trader=trader, clock=clock, jobs=jobs, store=store)
    return SimpleNamespace(make=make, db_path=tmp_duckdb_path)


def rows(trader):
    return {session: row for (_, session), row in sorted(trader.shadow_rows.items())}


@pytest.mark.timeout(120)
def test_every_due_session_is_replayed_on_the_bound_bar_size_with_warm_up(world):
    w = world.make("a.duckdb")
    w.replay.tick()
    got = rows(w.trader)
    assert list(got) == ["2024-03-18", "2024-03-19", "2024-03-20", "2024-03-21", "2024-03-22"]
    assert {size for size, _ in w.jobs} == {"15 mins"}
    first = got["2024-03-18"]
    assert first["status"] == "COMPLETE" and first["end_equity_usd"] - first["pnl_usd"] == pytest.approx(100_000.0)
    job = w.jobs[0][1]
    assert job.trading_start == dt.datetime(2024, 3, 18, tzinfo=UTC) and job.start < job.trading_start
    w.replay.tick()
    assert len(w.jobs) == 5                                                   # a repeat tick sends nothing


@pytest.mark.timeout(120)
def test_the_nightly_rows_equal_one_continuous_run(world):
    nightly = world.make("nightly.duckdb")
    nightly.clock["now"] = dt.datetime(2024, 3, 20, 23, tzinfo=UTC)
    nightly.replay.tick()
    nightly.clock["now"] = dt.datetime(2024, 3, 23, 12, tzinfo=UTC)
    nightly.replay.tick()
    continuous = world.make("continuous.duckdb")
    continuous.replay.tick()
    assert rows(nightly.trader) == rows(continuous.trader)


def _one_minute_bars(duckdb_path, conid, session):
    index = pd.date_range(f"{session} 13:30", f"{session} 19:59", freq="1min", tz="UTC")
    frame = pd.DataFrame({"open": 100.0, "high": 100.1, "low": 99.9, "close": 100.0, "volume": 1000.0,
                          "bar_size": "15 mins"}, index=index)
    frame.index.name = "date"
    DuckDBDataStore(duckdb_path).write(str(conid), frame)


@pytest.mark.timeout(120)
def test_wrong_size_or_missing_bars_make_an_incomplete_row_after_the_deadline(world):
    _one_minute_bars(world.db_path, CONIDS[0], "2024-03-21")
    w = world.make("b.duckdb", decided_at="2024-03-19T21:00:00+00:00")
    w.clock["now"] = dt.datetime(2024, 4, 2, 6, tzinfo=UTC)                  # 04-01 close + 16 h not reached
    w.replay.tick()
    got = rows(w.trader)
    assert got["2024-03-21"]["status"] == "INCOMPLETE" and got["2024-03-21"]["reason"].startswith("BAR_SIZE_MISMATCH")
    assert got["2024-03-28"]["status"] == "COMPLETE" and "2024-04-01" not in got
    w.clock["now"] = dt.datetime(2024, 4, 2, 13, tzinfo=UTC)
    w.replay.tick()
    assert rows(w.trader)["2024-04-01"]["reason"].startswith("BARS_MISSING")


def test_no_verdict_never_joins(world):
    w = world.make("c.duckdb", verdict="NO_VERDICT")
    w.replay.tick()
    assert w.trader.shadow_rows == {} and w.store.shadow_members() == []


@pytest.mark.timeout(120)
def test_a_final_refusal_is_a_visible_failure_and_never_marked_sent(world):      # ruling 25, PR #91 4218219293
    w = world.make("d.duckdb")
    first = ("jdg-00000001", "2024-03-18")
    w.trader.refuse_shadow[first] = ("SHADOW_SESSION_OUTSIDE_WINDOW", False)
    w.replay.tick()
    w.replay.tick()
    assert w.trader.shadow_calls.count(first) == 1                          # final: never sent again
    assert dt.date(2024, 3, 18) not in w.store.sent_sessions("jdg-00000001")
    (failure,) = w.store.shadow_failures()
    assert (str(failure["session_date"]), failure["code"]) == ("2024-03-18", "SHADOW_SESSION_OUTSIDE_WINDOW")
    assert w.replay.status()["failed_rows"] == 1
    assert sorted(rows(w.trader)) == ["2024-03-19", "2024-03-20", "2024-03-21", "2024-03-22"]


@pytest.mark.timeout(120)
def test_a_retryable_refusal_leaves_the_row_pending(world):
    w = world.make("e.duckdb")
    first = ("jdg-00000001", "2024-03-18")
    w.trader.refuse_shadow[first] = ("JUDGMENT_UNKNOWN", True)
    w.replay.tick()
    assert w.store.sent_sessions("jdg-00000001") == set() and w.store.shadow_failures() == []
    del w.trader.refuse_shadow[first]
    w.replay.tick()
    assert len(w.store.sent_sessions("jdg-00000001")) == 5 and w.replay.status()["failed_rows"] == 0
```

- [ ] **Step 2: Run** `.venv/bin/python -m pytest tests/research/test_shadow_replay.py -q` — expected: FAIL (`ModuleNotFoundError: trader.research.shadow_replay`).

- [ ] **Step 3: Implement.** `ResearchStore` additions:

```python
    def cases_without_member(self, since: dt.datetime) -> list[dict]:
        rows = self._db.execute(
            "SELECT c.case_digest, c.request_id FROM research_cases c LEFT JOIN shadow_members m "
            "ON m.case_digest = c.case_digest WHERE m.judgment_id IS NULL AND c.signed_at >= ? "
            "ORDER BY c.signed_at", [since], fetch="all")
        return [{"case_digest": r[0], "request_id": r[1]} for r in rows]

    def add_member(self, judgment_id: str, case_digest: str, verdict: str, decided_at: dt.datetime,
                   first_session: dt.date, last_session: dt.date, now: dt.datetime) -> None:
        """Spec 7: a judgment joins once, at its decided time; later judgments never change it."""
        def tx(conn):
            if conn.execute("SELECT 1 FROM shadow_members WHERE judgment_id = ?", [judgment_id]).fetchone() is None:
                conn.execute("INSERT INTO shadow_members VALUES (?, ?, ?, ?, ?, ?, ?)",
                             [judgment_id, case_digest, verdict, decided_at, first_session, last_session, now])
        self._db.transaction(tx)

    def shadow_members(self) -> list[dict]:
        names = ("judgment_id", "case_digest", "verdict", "decided_at", "first_session", "last_session")
        rows = self._db.execute(f"SELECT {', '.join(names)} FROM shadow_members ORDER BY joined_at, judgment_id",
                                fetch="all")
        return [dict(zip(names, r)) for r in rows]

    def sent_sessions(self, judgment_id: str) -> set:
        rows = self._db.execute("SELECT session_date FROM shadow_sent WHERE judgment_id = ?", [judgment_id],
                                fetch="all")
        return {r[0] for r in rows}

    def mark_sent(self, judgment_id: str, session: dt.date, status: str, reply_status: str, now: dt.datetime) -> None:
        """Ruling 25: only a row the trader stored (INSERTED, or DUPLICATE of the same body) is sent."""
        if reply_status not in ("INSERTED", "DUPLICATE"):
            raise ValueError(f"a {reply_status} reply is not a sent row")
        self._db.execute("INSERT INTO shadow_sent VALUES (?, ?, ?, ?, ?) ON CONFLICT DO NOTHING",
                         [judgment_id, session, status, reply_status, now])

    def mark_failed(self, judgment_id: str, session: dt.date, code: str, detail: Optional[str],
                    now: dt.datetime) -> None:
        self._db.execute("INSERT INTO shadow_failures VALUES (?, ?, ?, ?, ?) ON CONFLICT DO NOTHING",
                         [judgment_id, session, code, None if detail is None else str(detail)[:300], now])

    def failed_sessions(self, judgment_id: str) -> set:
        rows = self._db.execute("SELECT session_date FROM shadow_failures WHERE judgment_id = ?", [judgment_id],
                                fetch="all")
        return {r[0] for r in rows}

    def shadow_failures(self) -> list[dict]:
        names = ("judgment_id", "session_date", "code", "detail", "failed_at")
        rows = self._db.execute(f"SELECT {', '.join(names)} FROM shadow_failures ORDER BY failed_at, judgment_id",
                                fetch="all")
        return [dict(zip(names, r)) for r in rows]
```

`trader/research/shadow_replay.py`:

```python
"""Nightly shadow replay (SP2c spec 7): every judged strategy, on its own bound bar size, with the same
backtester, cost model and live rules; warm-up bars feed state only; each session's row is the change
since the previous row of one continuous run from the first post-verdict session."""
from __future__ import annotations

import datetime as dt
import hashlib
import logging
import re
import threading
from pathlib import Path
from typing import Any, Callable, Optional
from zoneinfo import ZoneInfo

import exchange_calendars as xcals
import pandas as pd

from trader.data.data_access import TickStorage
from trader.data.store import DateRange
from trader.objects import BarSize
from trader.research.evaluation import _day_end, _day_start, _point_key
from trader.research.evaluation_case import load_verified_case
from trader.research.evaluation_jobs import RunEnvironment, WindowJob, run_window_job
from trader.research.strategy_key import split_strategy_key
from trader.research.shadow_window import TRACKED_VERDICTS, shadow_window
from trader.research.trader_port import TraderUnavailable

logger = logging.getLogger(__name__)
NEW_YORK = ZoneInfo("America/New_York")
CALENDAR = xcals.get_calendar("XNYS")
JOIN_LOOKBACK = dt.timedelta(days=30)
TICK_SECONDS = 300.0
_BAR = re.compile(r"^(\d+) (sec|secs|min|mins|hour|hours)$")
_UNIT_SECONDS = {"sec": 1, "secs": 1, "min": 60, "mins": 60, "hour": 3600, "hours": 3600}


def bar_seconds(bar_size: str) -> int:
    match = _BAR.match(bar_size)
    if match is None:
        raise ValueError(f"unknown bar size {bar_size!r}")
    return int(match.group(1)) * _UNIT_SECONDS[match.group(2)]


def _ny_dates(index) -> list[dt.date]:
    stamps = pd.DatetimeIndex(index)
    stamps = stamps.tz_localize("UTC") if stamps.tz is None else stamps
    return list(stamps.tz_convert(NEW_YORK).date)


class _Wait(Exception):
    """The row is not final yet (an INCOMPLETE row waits for its deadline, ruling 11)."""


class ShadowReplay:
    def __init__(self, *, store: Any, trader: Any, signer: Any, artifacts_root: Path, paths: Any, registry: Any,
                 config: Any, judge: Any, now: Callable[[], dt.datetime], run_job=run_window_job):
        self._store, self._trader, self._signer = store, trader, signer
        self._cases_dir = Path(artifacts_root) / "cases"
        self._paths, self._registry, self._config, self._judge = paths, registry, config, judge
        self._now, self._run_job = now, run_job

    def serve_forever(self, stop: threading.Event) -> None:
        while not stop.is_set():
            try:
                self.tick()
            except TraderUnavailable as exc:
                logger.warning("shadow replay waits for the trader: %s", exc)
            except Exception:
                logger.exception("shadow replay tick failed")
            status = self.status()
            if status["failed_rows"]:
                logger.error("shadow replay: %d refused rows need the operator (shadow_failures)",
                             status["failed_rows"])
            stop.wait(TICK_SECONDS)

    def status(self) -> dict:
        """Ruling 25: refused rows are counted, never hidden among the sent ones."""
        return {"members": len(self._store.shadow_members()), "failed_rows": len(self._store.shadow_failures())}

    def tick(self) -> None:
        self._join_new_members()
        for member in self._store.shadow_members():
            self._replay(member)

    def _join_new_members(self) -> None:
        for row in self._store.cases_without_member(self._now() - JOIN_LOOKBACK):
            judgment = self._trader.judgment(case_digest=row["case_digest"])
            if judgment is None or judgment["verdict"] not in TRACKED_VERDICTS:
                continue                                             # unjudged yet, or NO_VERDICT (ruling 8)
            decided_at = dt.datetime.fromisoformat(judgment["body"]["decided_at"])
            first, last = shadow_window(decided_at, judgment["verdict"],
                                        deploy_expiry_sessions=self._judge.deploy_expiry_sessions,
                                        family_cooldown_sessions=self._judge.family_cooldown_sessions)
            self._store.add_member(judgment["judgment_id"], row["case_digest"], judgment["verdict"], decided_at,
                                   first, last, self._now())

    def _due_sessions(self, member: dict) -> list[dt.date]:
        last_closed = CALENDAR.minute_to_past_session(pd.Timestamp(self._now()), 1).date()
        end = min(member["last_session"], last_closed)
        if end < member["first_session"]:
            return []
        done = self._store.sent_sessions(member["judgment_id"]) | self._store.failed_sessions(member["judgment_id"])
        return [s.date() for s in CALENDAR.sessions_in_range(str(member["first_session"]), str(end))
                if s.date() not in done]

    def _replay(self, member: dict) -> None:
        case = load_verified_case(self._cases_dir, member["case_digest"],
                                  {self._signer.public_key_id: self._signer.public_key})
        for session in self._due_sessions(member):
            try:
                row = self._row(member, case, session)
            except _Wait:
                return                                               # later sessions wait too: rows stay ordered
            reply = self._trader.record_shadow(row)
            if reply["status"] in ("INSERTED", "DUPLICATE"):
                self._store.mark_sent(member["judgment_id"], session, row["status"], reply["status"], self._now())
                continue
            logger.error("shadow row %s %s refused: %s", member["judgment_id"], session, reply.get("code"))
            if reply.get("retryable"):
                return                                               # pending: the next tick sends it again
            self._store.mark_failed(member["judgment_id"], session, reply.get("code") or "REFUSED_WITHOUT_CODE",
                                    reply.get("detail"), self._now())

    def _incomplete(self, member: dict, case: Any, session: dt.date, reason: str) -> dict:
        deadline = CALENDAR.session_close(pd.Timestamp(session)) + pd.Timedelta(
            hours=self._config.shadow_incomplete_after_hours)
        if pd.Timestamp(self._now()) < deadline:
            raise _Wait(reason)
        return self._body(member, case, session, status="INCOMPLETE", reason=reason[:200])

    def _body(self, member: dict, case: Any, session: dt.date, **values: Any) -> dict:
        row = {"judgment_id": member["judgment_id"], "case_digest": member["case_digest"],
               "verdict": member["verdict"], "session_date": session.isoformat(), "bar_size": case.bar_size,
               "status": "COMPLETE", "reason": None, "pnl_usd": None, "fees_usd": None, "trades": None,
               "end_equity_usd": None}
        row.update(values)
        return row

    def _bar_problem(self, conids, bar_size: str, session: dt.date) -> Optional[str]:
        tickdata = TickStorage(self._paths.history_db).get_tickdata(BarSize.parse_str(bar_size))
        spacing = pd.Timedelta(seconds=bar_seconds(bar_size))
        for conid in conids:
            frame = tickdata.read(conid, date_range=DateRange(start=_day_start(session), end=_day_end(session)))
            if frame is None or len(frame) == 0:
                return f"BARS_MISSING: no {bar_size} bars for conid {conid} on {session}"
            gaps = pd.DatetimeIndex(frame.index).to_series().diff().dropna()
            if (gaps < spacing).any():
                return f"BAR_SIZE_MISMATCH: conid {conid} has bars closer than {bar_size} on {session}"
        return None

    def _environment(self, case: Any) -> RunEnvironment:
        family = None if case.family_id is None else self._registry.get_family(case.family_id)
        cost = family.cost_model if family is not None else {
            "order_notional": self._config.order_notional, "account_equity": self._config.account_equity,
            "max_gross_allocation": self._config.max_gross_allocation}
        path, class_name = split_strategy_key(case.strategy_key)
        return RunEnvironment(
            history_db=self._paths.history_db, universe_db=self._paths.universe_db,
            universe_library=self._paths.universe_library, execution_costs_path=self._paths.execution_costs,
            strategy_file=str(Path(self._paths.repo_root) / path), class_name=class_name,
            conids=tuple(case.conids), bar_size=case.bar_size, order_notional=float(cost["order_notional"]),
            account_equity=float(cost["account_equity"]),
            max_gross_allocation=float(cost["max_gross_allocation"]))

    def _row(self, member: dict, case: Any, session: dt.date) -> dict:
        path, _ = split_strategy_key(case.strategy_key)
        source = (Path(self._paths.repo_root) / path).read_bytes()
        if "sha256:" + hashlib.sha256(source).hexdigest() != case.strategy_file_hash:
            return self._incomplete(member, case, session, "STRATEGY_SOURCE_CHANGED")
        problem = self._bar_problem(case.conids, case.bar_size, session)
        if problem is not None:
            return self._incomplete(member, case, session, problem)
        evidence = case.evidence
        points = evidence.get("points") or []
        params = points[evidence["replay_index"]]["params"] if points else case.cohort[evidence["replay_index"]]
        first = pd.Timestamp(member["first_session"])
        warm_start = CALENDAR.session_offset(first, -int(evidence["warmup_sessions"])).date()
        env = self._environment(case)
        job = WindowJob(_point_key(params), dict(params), "shadow", 0, _day_start(warm_start), _day_end(session),
                        1.0, trading_start=_day_start(member["first_session"]))
        try:
            outcome = self._run_job(env, job)
        except Exception as exc:
            logger.exception("shadow replay of %s on %s failed", member["judgment_id"], session)
            return self._incomplete(member, case, session, f"REPLAY_ERROR: {type(exc).__name__}")
        curve = outcome.equity_series()
        dates = _ny_dates(curve.index)
        today = [value for day, value in zip(dates, curve.values) if day == session]
        if not today:
            return self._incomplete(member, case, session, f"BARS_MISSING: no equity on {session}")
        before = [value for day, value in zip(dates, curve.values) if day < session]
        start_equity = float(before[-1]) if before else env.account_equity
        trades = [t for t in outcome.trades if _ny_dates([t["timestamp"]])[0] == session]
        end_equity = float(today[-1])
        return self._body(member, case, session, pnl_usd=end_equity - start_equity,
                          fees_usd=float(sum(t["commission"] for t in trades)), trades=len(trades),
                          end_equity_usd=end_equity)
```

`trader/research_service.py` — in `build_runtime`, after `attest`:

```python
    shadow = ShadowReplay(store=store, trader=trader, signer=signer, artifacts_root=ARTIFACTS_ROOT, paths=paths,
                          registry=registry, config=config, judge=judge, now=now)
```

and `background=[evaluations.serve_forever, shadow.serve_forever]`.

- [ ] **Step 4: Run** `.venv/bin/python -m pytest tests/research/test_shadow_replay.py tests/research/test_evaluation_service.py -q` — expected: PASS.

- [ ] **Step 5: Commit** `feat: replay every judged strategy nightly on its own bar size`

```
feat: replay every judged strategy nightly on its own bar size

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
```

### Task 12: Docs and the full suite

**Files:** Modify `AGENTS.md`, `docs/ARCHITECTURE.md`, `docs/OPERATIONAL_STATE.md`.

- [ ] **Step 1: Docs.**
  - `AGENTS.md` Architecture: add a **research** bullet ("`trader.research_service`: claims evaluation slots at the trader, runs cohort evaluations, signs evaluation cases, turns a DEPLOY judgment into the paper `llm` review and a bundle, replays judged strategies nightly; holds the only research signing key"). Plan 1 already added `research` to the principal list; add two port rows: `| 42106 | Typed query (Ed25519) | research: get_evaluation |` and `| 42107 | Typed command (Ed25519) | research: submit_evaluation, attest_from_judgment |`.
  - `docs/ARCHITECTURE.md`: the same two port rows in the port table; a short "Research service (SP2c)" section: claim first, cohort → selection → one holdout, cases are signed with `mmr.research.evaluation-case.v1` and never authorize, `attest_from_judgment` is paper only, shadow books per verdict on the scoreboard; the `research_service:` config block.
  - `docs/OPERATIONAL_STATE.md` under "RPC keys": the `research` container is **not armed**. First start: `./docker.sh -k` (creates `research.key`), `~/.config/mmr/keys/private/signing.pem` must exist (mode 0600), `./docker.sh -K` must pass for `research` and every other service, then `docker compose --profile ai up -d research`. Open items: ruling 13 (DB volume exposure, owner to confirm), ruling 17 (renewal cases and the trader's forward evidence come with SP2c Plan 5), the signing-key file owner inside the container (the `0600` check in `load_signing_key` needs the container user to own the bind; check on the first start).
  - `docs/OPERATIONAL_STATE.md`, two watch items (rulings 24, 25): a finished evaluation shows as `RUNNING` until the trader confirmed its end (`research_requests.pending_report` is set meanwhile; the log says "not confirmed by the trader yet"); a shadow row the trader refused for good is in `shadow_failures` with its code, counted by the ERROR log "refused rows need the operator"; delete that row after fixing the cause and the next tick sends it again.

- [ ] **Step 2: Full suite** (once, under the shared lock):

```bash
until mkdir /private/tmp/mmr-suite.lock 2>/dev/null; do sleep 30; done
if .venv/bin/python -c "import xdist" 2>/dev/null; then
  .venv/bin/python -m pytest tests/ -q -n 8 --timeout=120 --timeout-method=thread \
    --ignore=tests/test_ibrx_async.py --basetemp=/private/tmp/mmr-suite-sp2c-03
else
  .venv/bin/python -m pytest tests/ -q --timeout=60 --timeout-method=thread \
    --ignore=tests/test_ibrx_async.py --basetemp=/private/tmp/mmr-suite-sp2c-03
fi
.venv/bin/python -m pytest tests/test_ibrx_async.py --timeout=30 -q
rmdir /private/tmp/mmr-suite.lock
```

Expected: PASS. Quote the live pytest summary line in the PR, not a remembered count.

- [ ] **Step 3: Commit** `docs: describe the research service, its ports and its first start`

```
docs: describe the research service, its ports and its first start

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
```

## Self-review

**Spec coverage.**

| Spec | Where |
|---|---|
| 5.1 identity, ports 42106/42107, `SERVER_ACCEPTS`/`CALLS`, `SERVICE_PRINCIPAL`, rotation, `KEYCHECK_SERVICES`, handler re-checks | Tasks 1, 2, 8 |
| 5.1 mounts (research; trader read-only `keys/verify`; ai `research.pub`), signing key only in research | Task 2; rulings 13–15 |
| 5.1 `submit_evaluation` steps 1–8 (code checks, request id, claim first, lost reply, restart, outage, state reports) | Tasks 3, 6 |
| 5.1 cohort run: every point full 1x/1.5x/2x + gate, neighbours 1x and never selectable, code selection, one holdout | Task 4 |
| 5.1 evaluation case (content, Plan 1's envelope and domain tag, refused by the bundle verifier) | Tasks 5, 6 |
| 5.1 `get_evaluation` (no bundle digest) | Task 6 |
| 5.1 `attest_from_judgment` steps 1–5 | Task 7 |
| 6.2 freeze, select on walk-forward, one disjoint holdout, `HOLDOUT_NOT_AVAILABLE`, `previously_revealed`, cross-family count | Tasks 3, 4 |
| 6.2 renewal case and forward evidence | not in this plan; SP2c Plan 5 (ruling 17) |
| 7 fixed cohort, own bar size, warm-up via `trading_start`, identity, books per verdict, INCOMPLETE | Tasks 9, 10, 11 |
| 8 failure rows for claims, outages, failed evaluations, attest refusals, shadow bars | Tasks 6, 7, 11 |
| 9 tests for this plan's blocks | Tasks 1–11 (see Review Focus) |

**Placeholders.** None: every step has real code or an exact edit. Two named dependencies are outside this plan: Plan 1's `case_digest` lookup (Plan 1 Task 6) and the renewal work (SP2c Plan 5, ruling 17).

**Names.** Spec names used exactly: `submit_evaluation`, `get_evaluation`, `attest_from_judgment`, `claim_evaluation`, `get_evaluation_claim`, `update_evaluation_claim`, `get_backtest_judgment`, `record_shadow_result`, `HOLDOUT_NOT_AVAILABLE`, `EVALUATION_REQUEST_CONFLICT`, `FAMILY_COOLING_DOWN`, `EVALUATION_LIMIT_REACHED`, `REVIEW_CONFLICT`, `mmr.research.evaluation-case.v1`, `max_cohort_points`, `shadow_warmup_sessions`, `trading_start`, `previously_revealed`. Plan 1 names used exactly: `EvaluationRequestBody`, `research_day`, `evaluation_request_id`, `EvaluationCase`, `write_evaluation_case`, `load_verified_case`, `offered_menu`, `FULL_MENU`, `split_strategy_key`, `BacktestJudgeConfig`, `COHORT_TOO_LARGE`, `STRATEGY_NOT_ALLOWED`. Journal migration 120, research migrations 20–22.

**Review Focus → tests.** 1: `test_evaluation_service.py` (refused claim, lost reply, restart, outage). 2: `test_cohort_evaluation.py`, `test_cohort_request.py`. 3: `test_case_builder.py::test_a_written_case_is_never_bundle_evidence` and the two menu tests. 4: `test_judgment_attest.py`. 5: `test_research_surface.py`, `test_research_principal.py::test_research_has_no_trading_policy_or_decision_right`, `test_backtester_trading_start.py`, `test_shadow_results.py`, `test_shadow_replay.py`.
