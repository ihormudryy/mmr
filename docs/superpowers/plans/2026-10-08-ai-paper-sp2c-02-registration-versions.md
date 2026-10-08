# AI Paper SP2c — Plan 2: Trader and strategy service: judgment-bound registration, deployment versions, entry rechecks — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** No strategy enters on paper under an AI deployment unless a durable DEPLOY judgment, a verified signed bundle and a sealed, active deployment version stand behind it. `register_ai_deployment` binds a DEPLOY judgment and a verified bundle and seals a deployment version. The operator can withdraw a version. The trader rechecks the version, the cap, the cooldown, the expiry and the source hash at admission and again right before the order is sent. Exits never depend on these checks. The strategy service loads active AI deployments from the exact bytes it hashed, on paper only, and stops a changed file.

**Architecture:** Trader side: one insert-only table `ai_deployment_versions` (migration 115) and one `ai_deployment_withdrawals` (116), both in the journal DuckDB. `DeploymentActivity` computes each version's status from the versions, the withdrawals, Plan 1's judgments and cooldowns, the XNYS calendar and the cap. `AiDeploymentRegistrar` reads the judgment, verifies the bundle with public keys (`ArtifactVerifier` + `require_qualified_research_evidence`), compares the binding three ways, then seals the base deployment and the version in one transaction. `AiPaperDecisionService._deployment` and a new dispatch `EntryGate` call `DeploymentActivity.entry_refusal`. Strategy side: `AiDeploymentSource` reads `get_active_ai_deployments` on each reconcile and loads or unloads instances keyed by the version digest. The `ai` controller only passes the signal's binding through to the ENTER body.

**Tech Stack:** Python 3.12, DuckDB (`SchemaMigrator`, `DuckDBConnection.transaction`), pydantic v2 strict wire models, `exchange_calendars` through `XNYSCalendarPolicy`, Ed25519 public-key verification (existing `ArtifactVerifier`), pytest. No new dependency.

**Spec:** `docs/superpowers/specs/2026-10-08-ai-paper-sp2c-backtest-judge-design.md` — binding sections 5.2 items 4–6 and 8, 5.4, 8, 9 ("Registration bound to the judgment", "Recheck at admission and dispatch", "Strategy binding", "Renewal version", "Access", "Bundle tampering"). Index: `docs/superpowers/plans/2026-10-08-ai-paper-sp2c-00-index.md`.

## Global Constraints

- **Base:** master after SP2c Plan 1 (judgments, cooldowns, `ai_paper.backtest_judge`). Code read at master 727d56e2.
- **Journal migrations:** this plan uses **115** (`ai_deployment_versions`) and **116** (`ai_deployment_withdrawals`). 117–119 stay unused. Owner rule (no legacy data): `ai_paper_decisions` (migration 56), `strategy_signal_record` (strategy service DB, no migrator) and `ai_opportunities` (`ai.duckdb` migration 14) are edited **in place**. No ALTER, no backfill. Operators start these DBs fresh.
- **Principals (spec 5.1 table, exact):** `get_active_ai_deployments` → `{strategy}`; `get_ai_deployment_version` → `{cli, dashboard, ai_supervisor, ai_research}`; `withdraw_ai_deployment` → `{cli, dashboard}`; `register_ai_deployment` stays `{ai_research}`. Every handler checks the principal again itself.
- **Wire models:** `ConfigDict(extra="forbid", strict=True)`. Digests match `^sha256:[0-9a-f]{64}$`. `True` is never an int. Dates on the wire are ISO `YYYY-MM-DD`.
- **DuckDB:** only through `DuckDBConnection.transaction` / `execute`. Inside a transaction callback never call another store's `db.execute` or `db.transaction` (the lock is not re-entrant). Read Plan 1's judgments **before** opening the registration transaction.
- **Public keys only.** The trader reads bundles from the artifacts directory and verify keys from `keys/verify/*.pem`. It never mounts or reads the signing key and never writes into the artifacts directory.
- **Paper only.** Registration refuses a live account (`ACCOUNT_NOT_PAPER`). The strategy service loads no AI deployment on a live account. No IB order is placed by any test.
- **Fail loudly:** every refusal has its own code (list in Cross-plan additions). Unknown digests are refused, never guessed. Conids compare as exact integers.
- **Never print secrets.** Refusal details name files and fields, never key bytes.
- Test-first. Per task run only the listed tests. Full suite once, in Task 11, under the shared lock.
- Commit subjects `feat:` / `fix:` / `refactor:` / `test:`, lowercase, imperative. Every commit message ends with a blank line and `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.
- No container restart, broker order, deploy or push is authorized by this plan.

## Rulings (spec silent, or the code forces a choice)

1. **Registration body and command id.** Body: `{"judgment_id", "bundle_digest", "deployment"}` (Plan 4 sends this). Today's `aidep-<base digest>` would make a renewal on the same bundle replay the INITIAL receipt. New id: `"aidep-" + sha256(canonical(body) + "|" + New York day)[:48]`. A same-day retry replays the ledger receipt; a different body for a bound judgment reaches the handler and gets `JUDGMENT_ALREADY_BOUND` (the ledger would otherwise answer `COMMAND_CONFLICT`); a next-day retry runs again, so Plan 4's "`DEPLOY_CAP_REACHED` waits for the next slot" works, and a next-day retry of a success returns the same digests from the stored `request_digest`. *Cost if wrong:* a refusal is final for the rest of that New York day.
2. **Bundle digest** = `"sha256:" + manifest_digest`; its directory is `<artifacts_root>/sha256_<hex>` (`bundle_dir_name`). `deployment.evidence_ref` must equal it. The trader re-checks that the verified manifest digest equals the name. *Cost if wrong:* none known; it is the exporter's own rule.
3. **Binding is normalized before it is compared:** file hash `"sha256:" + attested source digest`; bundle instruments (strings) → positive ints; params by canonical JSON (600 ≠ 600.0); bar size by `bar_size_key`; strategy path by `normalize_strategy_path`. Every difference is listed in one `JUDGMENT_MISMATCH` detail.
4. **"The bundle's review names this judgment":** `review.reviewer_kind == "llm"`, `review.reviewer == f"{model}#{judgment_id}"` with a non-empty model and the judgment id of the **initial** judgment of the line (for a RENEWAL, walk prior versions to the INITIAL one). *Cost if wrong:* a renewal would need a new review per renewal, which the exporter cannot add to an attested bundle.
5. **Sessions.** `first_session` = the first XNYS session strictly after the New York date of registration (research registers after the close). `expiry_session` = the `deploy_expiry_sessions`-th session counting `first_session` as 1, **inclusive**, but never later than the last session before the bundle attestation's New York expiry date. If that leaves no session → `BUNDLE_EXPIRED`. A version is expired when the New York date is after `expiry_session`. *Cost if wrong:* a mid-session registration waits one day.
6. **Status order** (first match wins): `WITHDRAWN`, `SUPERSEDED` (a later version names it as prior), `JUDGMENT_ENDED` (its judgment is gone, not DEPLOY, or a RENEWAL judgment naming it is not DEPLOY — `NO_VERDICT` included), `EXPIRED`, then the cap over the rest sorted by `(first_session, sealed_at, digest)`: the first `max_active_deploys` are `ACTIVE` (or `NOT_STARTED` before `first_session`), the rest `OVER_CAP`. `SUPERSEDED` outranks `EXPIRED` on purpose: spec 9 says "the old version stays expired", but Plan 5's renewal trigger fires on `EXPIRED` (Plan 4's controller ends lines on `EXPIRED`, `WITHDRAWN` and `ENDED`), so a renewed-then-expired version shown as expired would be renewed again every slot. It is `ENDED` on the wire and never trades either way. Registration counts `ACTIVE + NOT_STARTED` (excluding a renewal's prior) and refuses at the cap. Lowering the cap keeps the oldest. *Cost if wrong:* a cap change could flip which version trades.
7. **Renewal supersedes at once.** A RENEWAL judgment names `prior_version_digest`. Registration requires: the prior exists, is not withdrawn and was never renewed (`UNIQUE prior_version`), and the deployment body has the prior's base digest. It may happen before or after the prior's expiry; the new version starts at the next session; the prior becomes `SUPERSEDED`. *Cost if wrong:* an early renewal shortens nothing, it only moves the line forward under a fresh DEPLOY.
8. **Withdrawal** is per version, final and idempotent (a repeat returns `already_withdrawn: true`). A withdrawn version cannot be renewed. Any `cli`/`dashboard` operator may withdraw; no AI principal may.
9. **Decision fields.** `AiPaperDecision` always has `deployment_version` and `source_digest` (exact-key body; the wire defaults both to null). A strategy-kind ENTER needs both (`DEPLOYMENT_VERSION_REQUIRED`); a discretionary ENTER must have both null (`DEPLOYMENT_VERSION_UNEXPECTED`); a reduction must have both null (`DECISION_INVALID`).
10. **Source digest authority.** The strategy service hashes the executing bytes; the signal carries that digest; the controller copies it; the trader compares it with the base deployment's `strategy_digest`. The trader's check catches a stale or mismatched signal, not a lying controller (accepted: the controller is a signed principal, and the strategy service never loads changed bytes).
11. **Signal carries the base digest too.** `SignalEntry` gains `deployment_digest` besides the two spec fields, so the `ai` controller needs no extra read to build the ENTER. The trader still checks that the version belongs to that base digest.
12. **Recheck at dispatch** is an `EntryGate` composed into `ai_entry_gate` (after the discretionary gate). Reductions never reach it (the guard returns before AI gates for REDUCING orders). It reads the journal only, no IB. A gate exception becomes the guard's `AI_ENTRY_GATE_UNAVAILABLE`.
13. **Bundle verified at registration only.** Entry rechecks do not re-verify the bundle; ruling 5 bounds the version by the bundle's expiry. No revocation list exists on the trader yet (empty set, recorded as a known limit).
14. **Strategy instance identity.** Name `aidv-<first 16 hex of the version digest>`; the `aidv-` prefix is reserved (a config strategy with that prefix is refused). Module = `strategy_path` without the `strategies/` prefix, inside `strategies_directory`. `historical_days_prior = 5`, `paper_only = True`, `auto_execute = False`. Enabled on load unless the operator persisted it disabled. `update_strategy_params` on an `aidv-` name unloads the instance (the reload has no binding and is refused); the next reconcile loads it again from the trader's record.
15. **File changed after load:** every signal of that instance (BUY and SELL) is dropped, the instance is disabled at once and unloaded on the next reconcile. "Exits still go through" means the trader's brackets and the controller's CLOSE decisions, which never read the strategy instance (spec 5.4 test asserts the trader-side exit).
16. **Trader unreachable** during reconcile → keep the loaded set and log a warning (the trader still refuses entries). `METHOD_NOT_ALLOWED` (ai_paper off) → treated as no active deployment.
17. **Test seam, not a back door.** Harness tests that are not about registration seed a judged deployment straight into the stores with `tests/automation/judged_deployment.py`, and answer the judgment from a seeded reader installed by monkeypatching `judgment_reader_for`. Registration itself is tested through the real chain (Task 4 and Task 5 real-bundle tests).
18. **SP1 acceptance harness** (`trader/acceptance`) no longer registers a fixture deployment (the trader now refuses that). It takes an operator-given judged `deployment_version`, reads it, and sends ENTERs bound to it. *Owner to confirm.* *Cost if wrong:* a new SP1 acceptance run needs an SP2c-judged deployment first.
19. **`ai` controller pass-through, plus exit ownership by version.** The controller copies `deployment_digest`, `deployment_version` and `source_digest` from the signal to the ENTER and uses a new bracket section `decisions.ai_deployments` for `aidv-` strategies. The one other controller change is ruling 21 (exit ownership). Plan 4 owns the research cycle.
20. **Trader paths.** `Trader.research_artifacts_root` (default `~/.local/share/mmr/artifacts`) and `Trader.research_verify_dir` (default `~/.config/mmr/keys/verify`). Plan 1 added no trader attributes (its `default_cases_dir()` is `<artifacts_root>/cases`); Plan 2 builds Plan 1's `BacktestJudgments` with `cases_dir = <artifacts_root>/cases` and this verify dir, so both plans read the same files. The trader's compose service gets `keys/verify` read-only here (Plan 1 does not bind it; Plan 3 Task 2 keeps the one line).
21. **A SELL closes only trips its own version opened** (PR #91 thread 4218219168). Today `on_exit_signal` matches trips by conid only, so an A-bound SELL still queued when B supersedes A could close B's trip on the same conid. A trip belongs to the version of the ENTER that opened it: the controller reads that ENTER's `deployment_version` from its own `ai_submissions.body_json` by the trip's `decision_id` (a trip without a known ENTER counts as unbound, `None`). A SELL bound to version A (or unbound) owns exactly the open trips whose ENTER has the same version (or none). It closes only when every open trip of the experiment on that conid is its own; when another version also holds the conid it sends nothing and notes `EXIT_CONID_SHARED` (a CLOSE is conid-wide, and a PARTIAL_CLOSE would leave both brackets over a smaller position), so its own trip ends by its own bracket or the session flatten. The "may still fill" wait (`_fillable_entries`) counts only ENTERs of the SELL's own version. An exit never asks whether A is still active: the CLOSE carries no binding (ruling 9) and the trader's reduction path never reaches the version gate (ruling 12). *Cost if wrong:* an A trip that shares its conid with a B trip waits for its bracket or the 15:45 flatten instead of the strategy's SELL.

## Cross-plan additions

**Provided by Plan 2 (other plans use these exact names).**

- `register_ai_deployment` (command, `ai_research`). Request: `{"judgment_id": str (^[A-Za-z0-9_.:-]{1,128}$), "bundle_digest": "sha256:<64 hex>", "deployment": {AiDeployment fields}}`. RESOLVED outcome: `{"digest", "version_digest", "kind", "first_session", "expiry_session", "created": bool, "strategy_digest_provenance": "CLAIMED_NOT_VERIFIED"}`. Refusal codes: `JUDGMENT_MISSING`, `JUDGMENT_NOT_DEPLOY`, `JUDGMENT_MISMATCH`, `JUDGMENT_ALREADY_BOUND`, `DEPLOY_CAP_REACHED`, `FAMILY_COOLING_DOWN`, `BUNDLE_MISSING`, `BUNDLE_INVALID`, `BUNDLE_EXPIRED`, `BUNDLE_NOT_QUALIFIED`, `BUNDLE_KEYS_MISSING`, `RENEWAL_PRIOR_INVALID`, `ACCOUNT_NOT_PAPER`, `STYLE_NOT_ENABLED`, `DEPLOYMENT_INVALID`.
- `get_ai_deployment_version` (query). Request `{"version_digest"}`. Reply exactly Plan 4's strict `VersionReply` (a detailed `status` field would need a change to Plan 4's model): `{"found": bool, "version": {"version_digest", "base_digest", "judgment_id", "kind", "prior_version_digest", "first_session", "expiry_session", "state"}|null}`; `state` maps `ACTIVE|NOT_STARTED|OVER_CAP → "ACTIVE"`, `EXPIRED → "EXPIRED"`, `WITHDRAWN → "WITHDRAWN"`, `SUPERSEDED|JUDGMENT_ENDED → "ENDED"`.
- `withdraw_ai_deployment` (command, `cli`, `dashboard`). Request `{"version_digest", "reason" (1–200 chars)}`. Outcome `{"version_digest", "withdrawn": true, "already_withdrawn": bool}`; refusal `DEPLOYMENT_VERSION_UNKNOWN`.
- `get_active_ai_deployments` (query, `strategy`). Request `{}`. Reply `GetActiveAiDeploymentsResponse {"account_mode": "paper", "deployments": [ActiveAiDeployment]}`; `ActiveAiDeployment = {version_digest, base_digest, strategy_path, strategy_digest, class_name, params, conids, bar_size, expiry_session}` (`trader/messaging/ai_deployment_wire.py`).
- Entry refusal codes (admission and dispatch): `DEPLOYMENT_VERSION_REQUIRED`, `DEPLOYMENT_VERSION_UNEXPECTED`, `DEPLOYMENT_NOT_ACTIVE`, `DEPLOYMENT_EXPIRED`, `FAMILY_COOLING_DOWN`, `STRATEGY_SOURCE_MISMATCH`, `DEPLOYMENT_STATE_UNAVAILABLE` (retryable).
- `SignalEntry` / `read_ai_signals` signal keys gain `deployment_digest`, `deployment_version`, `source_digest` (all three null or all three set). `submit_ai_paper_decision` gains optional `deployment_version`, `source_digest` (default null).
- `trader.ai_deployment_versions` (the trader object) = `AiDeploymentVersionStore` with `version_for_judgment(judgment_id) -> Optional[str]` (Plan 3 consumes it). It exists only when `ai_paper.enabled` built the services; a reader guards with `getattr(trader, "ai_deployment_versions", None)` and refuses when it is missing.
- Domain tag `mmr.ai-deployment-version.v1`; `trader.automation.ai_deployment_versions.version_digest(version)`; kinds `INITIAL`, `RENEWAL`.
- `trader.strategy.ai_deployment_source.ai_instance_name(version_digest)`; test fixture `tests/strategy/ai_deployment_fixtures.py: StrategyNode(served)` with `.reconcile()`, `.instances() -> dict[version_digest, name]`, `.feed_bar(conid, frame)` (Plan 4 acceptance).
- `ai` config: `decisions.ai_deployments` (`Bracketing`, defaults 0.02 / 0.04).

**Used from Plan 1 (its real names).** Only `trader/automation/ai_judgment_port.py` and `_build_ai_paper_parts` use these names.

- `trader.automation.backtest_judgments.BacktestJudgments(db, *, config, calendar, cases_dir, verify_dir, now, renewals=None)` and `.get(judgment_id) -> BacktestJudgment | None` (raises `JudgmentRefused("JUDGMENT_TAMPERED")`). `BacktestJudgment` has `judgment_id, case_digest, request_id, kind, verdict, strategy_key, body, binding, cooldown_until_session, recorded_at`; `body["jev_model"]` is the model id and `body["renewal_of_version"]` the renewed version. Plan 2 builds it in `_build_ai_paper_parts` (moved from `_build_ai_paper_services`) so the dispatch gate can read it; `AiPaperServices.judgments` is the same object.
- `trader.research.evaluation_case`: `load_case_verify_keys(verify_dir)`, `load_verified_case(cases_dir, digest, keys) -> EvaluationCase` (raises `CaseRefused`). Header fields read: `strategy_key` (split by `trader.research.strategy_key.split_strategy_key`), `strategy_file_hash`, `selected_params`, `conids`, `bar_size`, `artifact_id`, `family_id`.
- Cooldown: `trader.automation.backtest_judge_schema.cooling_until_in_tx(conn, strategy_key, today) -> date | None` with `today = trader.automation.evaluation_claims.ny_day(now)`.
- Config: `AiPaperConfig.backtest_judge` (`BacktestJudgeConfig`), fields `max_active_deploys`, `deploy_expiry_sessions`.
- Renewal verdicts: Plan 1 refuses every RENEWAL judgment (`RENEWAL_NOT_SUPPORTED`) until SP2c Plan 5 (Renewal), so no judgment can name a version yet. `Plan1Judgments` takes a `renewals_of(version_digest)` read that defaults to none; Plan 5 supplies the real one, and Plan 5's `RenewalChecks` may call `trader.ai_deployment_versions.get(digest)` and `trader.ai_deployment_activity.status(digest)`.

## Review Focus

1. **Registration is bound to a DEPLOY judgment and a verified bundle.** REJECT → `JUDGMENT_NOT_DEPLOY`, none → `JUDGMENT_MISSING`, other params/conids/bar size/evaluation → `JUDGMENT_MISMATCH`, exact retry → same digests, other body → `JUDGMENT_ALREADY_BOUND`; a tampered, expired or unknown-key bundle is refused. → Task 5 `test_registration_refusals_bind_the_judgment`, `test_exact_retry_returns_the_same_versions`, `test_another_body_for_a_bound_judgment_is_refused`; Task 4 `test_tampered_expired_or_unknown_key_bundles_are_refused`; Task 5 `test_registration_through_a_real_signed_bundle`.
2. **Renewal gets a fresh version on the same base, refused after the bundle expires.** → Task 5 `test_renewal_gets_a_fresh_version_and_supersedes`, `test_renewal_is_refused_after_the_bundle_expires`, `test_the_same_renewal_twice_returns_the_same_version`.
3. **Entry recheck at admission and at dispatch, exits never blocked; a SELL closes only its own version's trips.** → Task 8 `test_expired_version_is_refused_at_admission`, `test_withdrawal_between_jev_and_send_is_refused_at_dispatch`, `test_expiry_between_jev_and_send_is_refused_at_dispatch`, `test_an_exit_on_the_same_conid_still_goes_out`; Task 10 `test_an_old_version_sell_never_closes_a_successor_trip`, `test_an_old_version_sell_still_closes_its_own_trip_after_supersession`.
4. **Strategy binding.** A changed file is refused at load; a file replaced after load drops signals and unloads; nothing loads on live; every signal of an AI instance carries the version. → Task 9 `test_a_changed_file_is_refused_at_load`, `test_a_file_replaced_after_load_drops_signals_and_unloads`, `test_nothing_loads_on_live`, `test_signals_carry_the_binding`.
5. **Cap and session boundaries.** Two concurrent registrations for the last slot → exactly one; expiry is inclusive and capped by the bundle. → Task 5 `test_two_concurrent_registrations_for_the_last_slot`; Task 3 `test_sessions_start_after_registration_and_end_inclusive`, `test_expiry_is_capped_by_the_bundle`.

## File map

| File | Responsibility | Task |
|---|---|---|
| `trader/automation/ai_deployment_versions.py`, `trader/automation/ai_deployments.py` | version + withdrawal tables, sealed version, in-tx base seal | 1 |
| `trader/automation/ai_judgment_port.py` | Plan 1 reads behind two small ports | 2 |
| `trader/automation/ai_deployment_activity.py`, `tests/automation/judged_deployment.py` | sessions, status, cap, entry refusal, dispatch gate; test fakes | 3 |
| `trader/automation/ai_bundle_check.py`, `trader/automation/strategy_binding.py` | bundle verification and facts | 4 |
| `trader/automation/ai_deployment_registration.py`, `trader/automation/ai_paper_actions.py` | registrar, withdraw, views | 5 |
| `trader/messaging/ai_deployment_wire.py`, `trader/messaging/production_api.py`, `trader/messaging/principals.py`, `trader/trading/command_stack.py`, `docker-compose.yml` | RPC, ACL, wiring | 6 |
| `trader/automation/ai_paper_decision.py` | decision fields, row columns, admission recheck | 7 |
| `trader/trading/command_stack.py`, `tests/automation/ai_paper_world.py` | dispatch gate wiring; recheck tests | 8 |
| `trader/data/strategy_signal_record.py`, `trader/strategy/ai_deployment_source.py`, `trader/strategy/strategy_runtime.py`, `tests/strategy/ai_deployment_fixtures.py` | signal binding, second source | 9 |
| `trader/ai/engine.py`, `trader/ai/signal_intake.py`, `trader/ai/runtime_schema.py`, `trader/ai/decision_engine.py`, `trader/ai/submitter.py`, `trader/ai/config.py` | controller pass-through, exit ownership by version | 10 |
| `trader/acceptance/scenario.py`, `trader/acceptance/runner.py`, `trader/mmr_cli.py`, tests | harness migration, full suite | 11 |

---

### Task 1: Deployment versions and withdrawals

**Files:**
- Create: `trader/automation/ai_deployment_versions.py`
- Modify: `trader/automation/ai_deployments.py` (`register_in_tx`; `_seal` split into `_seal_in_tx`)
- Test: `tests/automation/test_ai_deployment_versions.py`

**Interfaces:**
- Consumes: `SchemaMigrator.apply`, `canonical_json_bytes`, `DeploymentRefused`.
- Produces: `apply_ai_deployment_version_migrations(migrator) -> None`; `INITIAL`, `RENEWAL`; `DeploymentVersion(base_digest, judgment_id, kind, prior_version, first_session: date, expiry_session: date, binding_verified_by_bundle=True)` with `to_json()` / `from_json()`; `version_digest(v) -> str`; `SealedVersion(digest, version, request_digest, sealed_at)`; `AiDeploymentVersionStore(db, now)` with `seal_in_tx(conn, version, *, request_digest, principal, command_id) -> tuple[str, bool]`, `bound_to_judgment(judgment_id) -> Optional[SealedVersion]`, `version_for_judgment(judgment_id) -> Optional[str]`, `get(digest) -> DeploymentVersion`, `sealed() / sealed_in_tx(conn) -> tuple[SealedVersion, ...]`, `withdrawn() / withdrawn_in_tx(conn) -> frozenset[str]`, `withdraw(digest, *, reason, principal, command_id) -> bool`; `AiDeploymentStore.register_in_tx(conn, deployment, *, principal, command_id) -> tuple[str, bool]`.

- [ ] **Step 1: Write the failing tests**

```python
"""SP2c Plan 2 Task 1: sealed deployment versions (spec 5.2 item 5) and withdrawals."""
from __future__ import annotations

import datetime as dt

import pytest

from trader.automation.ai_deployment_versions import (
    INITIAL, RENEWAL, AiDeploymentVersionStore, DeploymentVersion, apply_ai_deployment_version_migrations,
    version_digest,
)
from trader.automation.ai_deployments import DeploymentRefused
from trader.data.duckdb_store import DuckDBConnection
from trader.data.schema_migrations import SchemaMigrator

NOW = dt.datetime(2026, 10, 9, 21, 0, tzinfo=dt.timezone.utc)
BASE = "sha256:" + "a" * 64


def version(judgment_id="jdg-1", kind=INITIAL, prior=None, first=dt.date(2026, 10, 12), expiry=dt.date(2026, 11, 6)):
    return DeploymentVersion(base_digest=BASE, judgment_id=judgment_id, kind=kind, prior_version=prior,
                             first_session=first, expiry_session=expiry)


@pytest.fixture
def store(tmp_path):
    db = DuckDBConnection.get_instance(str(tmp_path / "journal.duckdb"))
    apply_ai_deployment_version_migrations(SchemaMigrator(db))
    return AiDeploymentVersionStore(db, now=lambda: NOW)


def seal(store, v, request="sha256:" + "1" * 64):
    return store._db.transaction(lambda conn: store.seal_in_tx(conn, v, request_digest=request,
                                                                principal="ai_research", command_id="c"))


def test_version_digest_has_its_own_domain_and_is_rechecked_on_read(store):
    digest, created = seal(store, version())
    assert created and digest == version_digest(version()) and store.get(digest) == version()
    store._db.execute("UPDATE ai_deployment_versions SET record_json = replace(record_json, '2026-11-06', "
                      "'2026-12-31')")
    with pytest.raises(DeploymentRefused) as refused:
        store.get(digest)
    assert refused.value.code == "DEPLOYMENT_VERSION_TAMPERED"


def test_one_version_per_judgment(store):
    first, _ = seal(store, version())
    again, created = seal(store, version(first=dt.date(2026, 10, 13)))     # same request, a later day
    assert (again, created) == (first, False)
    with pytest.raises(DeploymentRefused) as refused:
        seal(store, version(first=dt.date(2026, 10, 13)), request="sha256:" + "2" * 64)
    assert refused.value.code == "JUDGMENT_ALREADY_BOUND"
    assert store.version_for_judgment("jdg-1") == first and store.version_for_judgment("jdg-x") is None
    other, created = seal(store, version("jdg-2"), request="sha256:" + "3" * 64)      # NULL priors coexist
    assert created and other != first


def test_a_renewal_gets_a_fresh_digest_and_a_prior_is_renewed_once(store):
    prior, _ = seal(store, version())
    renewal, _ = seal(store, version("jdg-2", RENEWAL, prior, dt.date(2026, 11, 9), dt.date(2026, 12, 7)))
    assert renewal != prior
    with pytest.raises(DeploymentRefused) as refused:
        seal(store, version("jdg-3", RENEWAL, prior, dt.date(2026, 11, 9), dt.date(2026, 12, 7)))
    assert refused.value.code == "RENEWAL_PRIOR_INVALID"


@pytest.mark.parametrize("change", [{"kind": "OTHER"}, {"kind": RENEWAL}, {"prior_version": BASE},
                                    {"expiry_session": dt.date(2026, 10, 1)},
                                    {"first_session": dt.datetime(2026, 10, 12)}, {"base_digest": "a" * 64},
                                    {"judgment_id": "has space"}, {"binding_verified_by_bundle": False}])
def test_bad_versions_are_refused(change):
    fields = {**version().__dict__, **change}
    with pytest.raises(DeploymentRefused):
        DeploymentVersion(**fields)


def test_withdraw_is_idempotent_and_needs_a_known_version(store):
    digest, _ = seal(store, version())
    assert store.withdraw(digest, reason="operator", principal="cli", command_id="w1") is True
    assert store.withdraw(digest, reason="again", principal="cli", command_id="w2") is False
    assert store.withdrawn() == frozenset({digest})
    with pytest.raises(DeploymentRefused) as refused:
        store.withdraw("sha256:" + "f" * 64, reason="x", principal="cli", command_id="w3")
    assert refused.value.code == "DEPLOYMENT_VERSION_UNKNOWN"
```

- [ ] **Step 2: Run** `.venv/bin/python -m pytest tests/automation/test_ai_deployment_versions.py -q` → fails (`ModuleNotFoundError`).

- [ ] **Step 3: Implement** `trader/automation/ai_deployment_versions.py`:

```python
"""Sealed deployment versions and operator withdrawals (SP2c spec 5.2 item 5).

A version binds one DEPLOY judgment to one base deployment for a window of
sessions. The base ``ai_deployments`` row keeps its SP1 meaning; only the
version is authority in SP2c. Both tables are insert-only.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import hmac
import json
import re
from dataclasses import dataclass, fields
from typing import Any, Callable, Optional

from trader.automation.ai_deployments import DeploymentRefused
from trader.data.schema_migrations import SchemaMigrator
from trader.research.canonical import canonical_json_bytes

AI_DEPLOYMENT_VERSION_MIGRATION_VERSION = 115
AI_DEPLOYMENT_WITHDRAWAL_MIGRATION_VERSION = 116
INITIAL = "INITIAL"
RENEWAL = "RENEWAL"
_VERSION_DOMAIN = b"mmr.ai-deployment-version.v1\x00"
_SHA256 = re.compile(r"^sha256:[0-9a-f]{64}$")
_JUDGMENT_ID = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")
_COLUMNS = "digest, record_json, request_digest, sealed_at"


def apply_ai_deployment_version_migrations(migrator: SchemaMigrator) -> None:
    migrator.apply(AI_DEPLOYMENT_VERSION_MIGRATION_VERSION, "sp2c_ai_deployment_versions", (
        """CREATE TABLE IF NOT EXISTS ai_deployment_versions (
            digest VARCHAR PRIMARY KEY, judgment_id VARCHAR NOT NULL UNIQUE, base_digest VARCHAR NOT NULL,
            prior_version VARCHAR UNIQUE, record_json VARCHAR NOT NULL, request_digest VARCHAR NOT NULL,
            principal VARCHAR NOT NULL, command_id VARCHAR NOT NULL, sealed_at TIMESTAMPTZ NOT NULL)""",))
    migrator.apply(AI_DEPLOYMENT_WITHDRAWAL_MIGRATION_VERSION, "sp2c_ai_deployment_withdrawals", (
        """CREATE TABLE IF NOT EXISTS ai_deployment_withdrawals (
            version_digest VARCHAR PRIMARY KEY, reason VARCHAR NOT NULL, principal VARCHAR NOT NULL,
            command_id VARCHAR NOT NULL, withdrawn_at TIMESTAMPTZ NOT NULL)""",))


def _invalid(rule: str) -> DeploymentRefused:
    return DeploymentRefused("DEPLOYMENT_VERSION_INVALID", rule)


@dataclass(frozen=True)
class DeploymentVersion:
    base_digest: str
    judgment_id: str
    kind: str
    prior_version: Optional[str]
    first_session: dt.date
    expiry_session: dt.date
    binding_verified_by_bundle: bool = True

    def __post_init__(self):
        if not isinstance(self.base_digest, str) or not _SHA256.match(self.base_digest):
            raise _invalid("base_digest must be sha256:<64 hex>")
        if not isinstance(self.judgment_id, str) or not _JUDGMENT_ID.match(self.judgment_id):
            raise _invalid("judgment_id must match ^[A-Za-z0-9_.:-]{1,128}$")
        if self.kind not in (INITIAL, RENEWAL):
            raise _invalid("kind must be INITIAL or RENEWAL")
        if (self.kind == INITIAL) != (self.prior_version is None):
            raise _invalid("an INITIAL version has no prior version; a RENEWAL names one")
        if self.prior_version is not None and (not isinstance(self.prior_version, str)
                                               or not _SHA256.match(self.prior_version)):
            raise _invalid("prior_version must be sha256:<64 hex>")
        if type(self.first_session) is not dt.date or type(self.expiry_session) is not dt.date:
            raise _invalid("sessions must be dates, not datetimes")     # datetime is a date subclass
        if self.expiry_session < self.first_session:
            raise _invalid("expiry_session is before first_session")
        if self.binding_verified_by_bundle is not True:
            raise _invalid("a version exists only after the bundle check")

    def to_json(self) -> dict:
        record = {f.name: getattr(self, f.name) for f in fields(self)}
        record["first_session"] = self.first_session.isoformat()
        record["expiry_session"] = self.expiry_session.isoformat()
        return record

    @classmethod
    def from_json(cls, value: Any) -> "DeploymentVersion":
        names = {f.name for f in fields(cls)}
        if not isinstance(value, dict) or set(value) != names:
            raise _invalid(f"a version has exactly the keys {sorted(names)}")
        try:
            sessions = {k: dt.date.fromisoformat(value[k]) for k in ("first_session", "expiry_session")}
        except (TypeError, ValueError):
            raise _invalid("sessions must be ISO dates") from None
        return cls(**{**value, **sessions})


def version_digest(version: DeploymentVersion) -> str:
    return "sha256:" + hashlib.sha256(_VERSION_DOMAIN + canonical_json_bytes(version.to_json())).hexdigest()


@dataclass(frozen=True)
class SealedVersion:
    digest: str
    version: DeploymentVersion
    request_digest: str
    sealed_at: dt.datetime


class AiDeploymentVersionStore:
    def __init__(self, db: Any, now: Callable[[], dt.datetime]):
        self._db = db
        self._now = now

    def seal_in_tx(self, conn, version: DeploymentVersion, *, request_digest: str, principal: str,
                   command_id: str) -> tuple[str, bool]:
        """One version per judgment. The same registration request returns the first version, even when
        its sessions would differ today; another request for a bound judgment is refused."""
        bound = conn.execute("SELECT digest, request_digest FROM ai_deployment_versions WHERE judgment_id = ?",
                             [version.judgment_id]).fetchone()
        if bound is not None:
            if hmac.compare_digest(bound[1], request_digest):
                return bound[0], False
            raise DeploymentRefused("JUDGMENT_ALREADY_BOUND", f"judgment {version.judgment_id} has a version")
        if version.prior_version is not None and conn.execute(
                "SELECT 1 FROM ai_deployment_versions WHERE prior_version = ?", [version.prior_version]).fetchone():
            raise DeploymentRefused("RENEWAL_PRIOR_INVALID", "the prior version was already renewed")
        digest = version_digest(version)
        conn.execute(
            "INSERT INTO ai_deployment_versions (digest, judgment_id, base_digest, prior_version, record_json, "
            "request_digest, principal, command_id, sealed_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [digest, version.judgment_id, version.base_digest, version.prior_version,
             canonical_json_bytes(version.to_json()).decode("utf-8"), request_digest, principal, command_id,
             self._now()])
        return digest, True

    def _parse(self, row: tuple) -> SealedVersion:
        digest, record_json, request_digest, sealed_at = row
        try:
            version = DeploymentVersion.from_json(json.loads(record_json))
        except (DeploymentRefused, ValueError):
            raise DeploymentRefused("DEPLOYMENT_VERSION_TAMPERED", f"{digest} does not parse") from None
        if not hmac.compare_digest(version_digest(version), digest):
            raise DeploymentRefused("DEPLOYMENT_VERSION_TAMPERED", f"{digest} does not match its record")
        return SealedVersion(digest, version, request_digest, sealed_at)

```

The remaining store methods are plain reads through `_parse` (so every read re-checks the digest): `bound_to_judgment` (`WHERE judgment_id = ?`, `fetch="one"`), `version_for_judgment` (its digest or None), `get` (a non-digest or a missing row → `DEPLOYMENT_VERSION_UNKNOWN`), `sealed_in_tx` / `sealed` (`ORDER BY sealed_at, digest`), `withdrawn_in_tx` / `withdrawn` (the set of `version_digest`). `withdraw` runs one transaction: unknown version → `DEPLOYMENT_VERSION_UNKNOWN`; already withdrawn → `False`; else insert `(digest, reason, principal, command_id, now)` → `True`.

In `ai_deployments.py`: rename `_seal````

In `ai_deployments.py`: rename `_seal` to `_seal_in_tx(self, conn, digest, record, kind, provenance, principal, command_id) -> bool` (the body of today's `write`), make `register` / `register_discretionary` call `self._db.transaction(lambda conn: ...)`, and add:

```python
    def register_in_tx(self, conn, deployment: AiDeployment, *, principal: str, command_id: str) -> tuple[str, bool]:
        """The base row inside the caller's transaction, so a version is never sealed without it."""
        if not isinstance(deployment, AiDeployment):
            raise DeploymentRefused("DEPLOYMENT_INVALID", "deployment must be AiDeployment")
        digest = deployment_digest(deployment)
        return digest, self._seal_in_tx(conn, digest, deployment.to_json(), STRATEGY_KIND,
                                        STRATEGY_DIGEST_PROVENANCE, principal, command_id)
```

Also fix the module docstring: "``strategy_digest`` is a claim of ``ai_research`` on this row; SP2c records the verified binding on the deployment version."

- [ ] **Step 4: Run** `tests/automation/test_ai_deployment_versions.py tests/automation/test_ai_deployments.py` → pass.
- [ ] **Step 5: Commit** `feat: add sealed ai deployment versions and withdrawals`.

### Task 2: Ports to Plan 1's judgments and cooldowns

**Files:**
- Create: `trader/automation/ai_judgment_port.py`
- Test: `tests/automation/test_ai_judgment_port.py`

**Interfaces:**
- Consumes (Plan 1, see Cross-plan additions): `BacktestJudgments.get`, `BacktestJudgment` (`judgment_id`, `case_digest`, `kind`, `verdict`, `body["jev_model"]`, `body["renewal_of_version"]`), `trader.research.evaluation_case.load_case_verify_keys / load_verified_case`, `EvaluationCase` header fields, `split_strategy_key`, `cooling_until_in_tx`, `ny_day`.
- Produces: `JudgmentFacts` (frozen: `judgment_id, verdict, kind, renews_version, model_id, strategy_path, class_name, file_hash, params, conids: tuple[int, ...], bar_size, artifact_id: Optional[str], family_id: Optional[str]`); `JudgmentReader` protocol (`get`, `renewal_verdicts`); `CooldownReader` protocol (`cooling_down(strategy_key, now) -> bool`); `strategy_key(path, class_name) -> str`; `judgment_reader_for(judgments, *, cases_dir, verify_dir) -> JudgmentReader`; `cooldown_reader_for(db) -> CooldownReader`.

- [ ] **Step 1: Read Plan 1's merged store and case module.** If a name differs from the Cross-plan list, change only this module and the `SimpleNamespace` stubs in its test.
- [ ] **Step 2: Write the failing test**

```python
"""SP2c Plan 2 Task 2: Plan 1 judgments and cooldowns behind two ports."""
import datetime as dt
from types import SimpleNamespace

from trader.automation.ai_judgment_port import Plan1Cooldowns, Plan1Judgments, strategy_key

CASE = SimpleNamespace(strategy_key="strategies/orb.py:Orb", strategy_file_hash="sha256:" + "a" * 64,
                       selected_params={"RANGE_MINUTES": 15}, conids=[265598, 272093], bar_size="1 min",
                       artifact_id="art-1", family_id="fam-1")


def judgment(judgment_id, verdict="DEPLOY", kind="INITIAL", prior=None):
    """The fields of Plan 1's BacktestJudgment this port reads."""
    return SimpleNamespace(judgment_id=judgment_id, case_digest="sha256:" + "c" * 64, kind=kind, verdict=verdict,
                           body={"jev_model": "openrouter/jev", "renewal_of_version": prior,
                                 "decided_at": "2026-10-09T21:00:00+00:00"})


def test_facts_join_the_judgment_and_its_verified_case():
    store = SimpleNamespace(get=lambda j: judgment(j) if j == "jdg-1" else None)
    reader = Plan1Judgments(store, read_case=lambda digest: CASE,
                            renewals_of=lambda v: [judgment("jdg-2", "SHADOW", "RENEWAL", v)])
    facts = reader.get("jdg-1")
    assert (facts.verdict, facts.model_id, facts.conids, facts.file_hash) == (
        "DEPLOY", "openrouter/jev", (265598, 272093), CASE.strategy_file_hash)
    assert (facts.strategy_path, facts.class_name, facts.params) == ("strategies/orb.py", "Orb", {"RANGE_MINUTES": 15})
    assert reader.get("jdg-x") is None
    assert reader.renewal_verdicts("sha256:" + "d" * 64) == ("SHADOW",)


def test_no_renewal_verdict_exists_before_plan_5():
    reader = Plan1Judgments(SimpleNamespace(get=lambda j: None), read_case=lambda digest: CASE)
    assert reader.renewal_verdicts("sha256:" + "d" * 64) == ()


def test_cooldown_reads_plan_1_in_one_transaction(monkeypatch):
    import trader.automation.ai_judgment_port as port
    seen = []

    def cooling_until(conn, key, today):
        seen.append((key, today))
        return dt.date(2026, 10, 22) if key == "k" else None
    monkeypatch.setattr(port, "cooling_until_in_tx", cooling_until)
    db = SimpleNamespace(transaction=lambda fn: fn("conn"))
    cooldowns = Plan1Cooldowns(db)
    late_evening = dt.datetime(2026, 10, 9, 1, 30, tzinfo=dt.timezone.utc)          # still 8 Oct in New York
    assert cooldowns.cooling_down("k", late_evening) is True
    assert cooldowns.cooling_down("other", late_evening) is False
    assert seen[0] == ("k", dt.date(2026, 10, 8))


def test_strategy_key_is_file_and_class():
    assert strategy_key("strategies/x.py", "Foo") == "strategies/x.py:Foo"
```

- [ ] **Step 3: Run** → fails (`ModuleNotFoundError`).
- [ ] **Step 4: Implement**

```python
"""What Plan 2 reads from Plan 1: durable judgments (joined with their verified case) and cooldowns.

Every Plan 1 name this plan relies on is used only here.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Optional, Protocol

from trader.automation.backtest_judge_schema import cooling_until_in_tx
from trader.automation.evaluation_claims import ny_day
from trader.research.strategy_key import split_strategy_key


@dataclass(frozen=True)
class JudgmentFacts:
    judgment_id: str
    verdict: str                   # DEPLOY | SHADOW | REJECT | NO_VERDICT
    kind: str                      # INITIAL | RENEWAL
    renews_version: Optional[str]
    model_id: str
    strategy_path: str
    class_name: str
    file_hash: str                 # sha256:<hex> of the evaluated bytes
    params: Mapping[str, Any]
    conids: tuple[int, ...]
    bar_size: str
    artifact_id: Optional[str]     # None for a RENEWAL case
    family_id: Optional[str]


class JudgmentReader(Protocol):
    def get(self, judgment_id: str) -> Optional[JudgmentFacts]: ...
    def renewal_verdicts(self, version_digest: str) -> tuple[str, ...]: ...


class CooldownReader(Protocol):
    def cooling_down(self, strategy_key: str, now: dt.datetime) -> bool: ...


def strategy_key(strategy_path: str, class_name: str) -> str:
    return f"{strategy_path}:{class_name}"


def _no_renewals(version_digest: str) -> Iterable[Any]:
    """Plan 1 refuses every RENEWAL judgment until SP2c Plan 5, so none can name a version."""
    return ()


class Plan1Judgments:
    def __init__(self, store: Any, *, read_case: Callable[[str], Any],
                 renewals_of: Callable[[str], Iterable[Any]] = _no_renewals):
        self._store = store
        self._read_case = read_case
        self._renewals_of = renewals_of

    def get(self, judgment_id: str) -> Optional[JudgmentFacts]:
        judgment = self._store.get(judgment_id)        # BacktestJudgment; a tampered row raises JudgmentRefused
        if judgment is None:
            return None
        case = self._read_case(judgment.case_digest)   # EvaluationCase; signature checked by Plan 1's verifier
        strategy_path, class_name = split_strategy_key(case.strategy_key)
        return JudgmentFacts(
            judgment_id=judgment.judgment_id, verdict=judgment.verdict, kind=judgment.kind,
            renews_version=judgment.body["renewal_of_version"], model_id=judgment.body["jev_model"],
            strategy_path=strategy_path, class_name=class_name, file_hash=case.strategy_file_hash,
            params=dict(case.selected_params or {}), conids=tuple(sorted(int(c) for c in case.conids)),
            bar_size=case.bar_size, artifact_id=case.artifact_id, family_id=case.family_id)

    def renewal_verdicts(self, version_digest: str) -> tuple[str, ...]:
        return tuple(judgment.verdict for judgment in self._renewals_of(version_digest))


class Plan1Cooldowns:
    def __init__(self, db: Any):
        self._db = db

    def cooling_down(self, key: str, now: dt.datetime) -> bool:
        today = ny_day(now)
        return self._db.transaction(lambda conn: cooling_until_in_tx(conn, key, today)) is not None


def judgment_reader_for(judgments: Any, *, cases_dir: Path, verify_dir: Path) -> JudgmentReader:
    from trader.research.evaluation_case import load_case_verify_keys, load_verified_case

    def read_case(digest: str) -> Any:
        # Keys are loaded per read: nothing touches the disk at trader start, and a rotated key is seen.
        return load_verified_case(cases_dir, digest, load_case_verify_keys(verify_dir))
    return Plan1Judgments(judgments, read_case=read_case)


def cooldown_reader_for(db: Any) -> CooldownReader:
    return Plan1Cooldowns(db)
```

- [ ] **Step 5: Run** → pass. **Commit** `feat: read plan 1 judgments and cooldowns through two ports`.

### Task 3: Sessions, status, cap, entry refusal and the dispatch gate

**Files:**
- Create: `trader/automation/ai_deployment_activity.py`, `tests/automation/judged_deployment.py`
- Test: `tests/automation/test_ai_deployment_activity.py`

**Interfaces:**
- Consumes: `AiDeploymentVersionStore`, `AiDeploymentStore.get_sealed / kind_of`, `JudgmentReader`, `CooldownReader`, `XNYSCalendarPolicy.sessions_in_range`.
- Produces: `ny_date(moment) -> date`; `deployment_sessions(calendar, *, registered_at, sessions, bundle_expires_at) -> tuple[date, date]`; statuses `ACTIVE, NOT_STARTED, EXPIRED, WITHDRAWN, SUPERSEDED, JUDGMENT_ENDED, OVER_CAP`; `classify(sealed, *, withdrawn, stands, today, max_active, unknown_stands=False) -> dict[str, str]`; `ActiveDeployment(version_digest, version, deployment)`; `DeploymentActivity(versions, deployments, judgments, cooldowns, max_active, now)` with `stands_for(sealed) -> dict[str, bool]`, `statuses()`, `status(digest)`, `active()`, `entry_refusal(*, deployment_digest, version_digest, source_digest) -> Optional[str]`; `deployment_version_gate(*, kind_of, activity) -> EntryGate`. Test helpers: `SeededJudgments`, `Cooldowns`, `deploy_facts(record, judgment_id, model_id="jev-model")`.

- [ ] **Step 1: Write the test helpers** `tests/automation/judged_deployment.py` (Task 11 adds `seed_judged_deployment`):

```python
"""Tests only: judgment facts and cooldowns for stacks that do not run the research chain."""
from __future__ import annotations

from trader.automation.ai_judgment_port import JudgmentFacts


class SeededJudgments:
    def __init__(self, inner=None):
        self._facts, self._renewals, self._inner = {}, {}, inner

    def seed(self, facts: JudgmentFacts) -> None:
        self._facts[facts.judgment_id] = facts

    def end_line(self, version_digest: str, verdict: str = "SHADOW") -> None:
        self._renewals[version_digest] = self._renewals.get(version_digest, ()) + (verdict,)

    def get(self, judgment_id):
        found = self._facts.get(judgment_id)
        return found if found is not None or self._inner is None else self._inner.get(judgment_id)

    def renewal_verdicts(self, version_digest):
        inner = () if self._inner is None else self._inner.renewal_verdicts(version_digest)
        return self._renewals.get(version_digest, ()) + tuple(inner)


class Cooldowns:
    def __init__(self):
        self.keys: set[str] = set()

    def cooling_down(self, key, now):
        return key in self.keys


def deploy_facts(record: dict, judgment_id: str, *, verdict="DEPLOY", kind="INITIAL", renews=None,
                 model_id="jev-model", artifact_id="art-1", family_id="fam-1") -> JudgmentFacts:
    initial = kind == "INITIAL"
    return JudgmentFacts(judgment_id, verdict, kind, renews, model_id, record["strategy_path"],
                         record["class_name"], record["strategy_digest"], dict(record["params"]),
                         tuple(sorted(record["conids"])), record["bar_size"],
                         artifact_id if initial else None, family_id if initial else None)
```

- [ ] **Step 2: Write the failing tests** (`tests/automation/test_ai_deployment_activity.py`):

```python
"""SP2c Plan 2 Task 3: sessions, status, cap and the entry refusal (spec 5.2 items 5 and 8)."""
from __future__ import annotations

import datetime as dt

import pytest

from tests.automation.judged_deployment import Cooldowns, SeededJudgments, deploy_facts
from trader.automation.ai_deployment_activity import (
    ACTIVE, EXPIRED, JUDGMENT_ENDED, NOT_STARTED, OVER_CAP, SUPERSEDED, WITHDRAWN, DeploymentActivity,
    deployment_sessions,
)
from trader.automation.ai_deployment_versions import (
    INITIAL, RENEWAL, AiDeploymentVersionStore, DeploymentVersion, apply_ai_deployment_version_migrations,
)
from trader.automation.ai_deployments import (
    AiDeployment, AiDeploymentStore, DeploymentRefused, apply_ai_deployment_migration,
)
from trader.automation.calendar_policy import XNYSCalendarPolicy
from trader.data.duckdb_store import DuckDBConnection
from trader.data.schema_migrations import SchemaMigrator

UTC = dt.timezone.utc
FRIDAY_EVENING = dt.datetime(2026, 10, 9, 21, 0, tzinfo=UTC)
RECORD = {"strategy_path": "strategies/opening_range_breakout.py", "strategy_digest": "sha256:" + "a" * 64,
          "class_name": "OpeningRangeBreakout", "params": {"RANGE_MINUTES": 15}, "conids": [265598, 272093],
          "bar_size": "1 min", "style": "intraday_long", "decider": "jev", "decider_verdict": "DEPLOY",
          "evidence_ref": "sha256:" + "b" * 64, "evidence_order_notional": 2000.0}


def test_sessions_start_after_registration_and_end_inclusive():
    first, expiry = deployment_sessions(XNYSCalendarPolicy(), registered_at=FRIDAY_EVENING, sessions=20,
                                        bundle_expires_at=FRIDAY_EVENING + dt.timedelta(days=90))
    assert (first, expiry) == (dt.date(2026, 10, 12), dt.date(2026, 11, 6))


def test_expiry_is_capped_by_the_bundle():
    _, expiry = deployment_sessions(XNYSCalendarPolicy(), registered_at=FRIDAY_EVENING, sessions=20,
                                    bundle_expires_at=dt.datetime(2026, 10, 20, 12, tzinfo=UTC))
    assert expiry == dt.date(2026, 10, 19)
    with pytest.raises(DeploymentRefused) as refused:
        deployment_sessions(XNYSCalendarPolicy(), registered_at=FRIDAY_EVENING, sessions=20,
                            bundle_expires_at=dt.datetime(2026, 10, 12, 12, tzinfo=UTC))
    assert refused.value.code == "BUNDLE_EXPIRED"


class World:
    def __init__(self, tmp_path, max_active=3):
        self.now = dt.datetime(2026, 10, 14, 15, 0, tzinfo=UTC)
        db = DuckDBConnection.get_instance(str(tmp_path / "journal.duckdb"))
        migrator = SchemaMigrator(db)
        apply_ai_deployment_migration(migrator)
        apply_ai_deployment_version_migrations(migrator)
        self.db, self.judgments, self.cooldowns = db, SeededJudgments(), Cooldowns()
        self.deployments = AiDeploymentStore(db, now=lambda: self.now)
        self.versions = AiDeploymentVersionStore(db, now=lambda: self.now)
        self.activity = DeploymentActivity(versions=self.versions, deployments=self.deployments,
                                           judgments=self.judgments, cooldowns=self.cooldowns,
                                           max_active=max_active, now=lambda: self.now)

    def seal(self, n, *, first=dt.date(2026, 10, 12), expiry=dt.date(2026, 11, 6), kind=INITIAL, prior=None):
        record = {**RECORD, "conids": [265598 + n]}
        self.judgments.seed(deploy_facts(record, f"jdg-{n}", kind=kind, renews=prior))
        deployment = AiDeployment.from_json(record)
        version = lambda base: DeploymentVersion(base, f"jdg-{n}", kind, prior, first, expiry)

        def write(conn):
            base, _ = self.deployments.register_in_tx(conn, deployment, principal="ai_research", command_id="c")
            return self.versions.seal_in_tx(conn, version(base), request_digest=f"sha256:{n:064x}",
                                            principal="ai_research", command_id="c")[0]
        return self.db.transaction(write), record


@pytest.fixture
def world(tmp_path):
    return World(tmp_path)


def test_status_order(world):
    withdrawn, _ = world.seal(1)
    world.versions.withdraw(withdrawn, reason="x", principal="cli", command_id="w")
    renewed, _ = world.seal(2)
    world.seal(3, kind=RENEWAL, prior=renewed, first=dt.date(2026, 10, 15))
    ended, _ = world.seal(4)
    world.judgments.end_line(ended, "NO_VERDICT")
    old, _ = world.seal(5, first=dt.date(2026, 9, 1), expiry=dt.date(2026, 10, 13))
    statuses = world.activity.statuses()
    assert [statuses[d] for d in (withdrawn, renewed, ended, old)] == [WITHDRAWN, SUPERSEDED, JUDGMENT_ENDED,
                                                                       EXPIRED]


def test_expiry_session_is_inclusive_and_not_started_waits(world):
    today, _ = world.seal(1, expiry=dt.date(2026, 10, 14))
    later, _ = world.seal(2, first=dt.date(2026, 10, 15))
    assert world.activity.statuses() == {today: ACTIVE, later: NOT_STARTED}


def test_a_lowered_cap_keeps_the_oldest(tmp_path):
    world = World(tmp_path, max_active=1)
    older, _ = world.seal(1, first=dt.date(2026, 10, 12))
    newer, _ = world.seal(2, first=dt.date(2026, 10, 13))
    assert world.activity.statuses() == {older: ACTIVE, newer: OVER_CAP}
    assert [a.version_digest for a in world.activity.active()] == [older]


def test_entry_refusal_codes(world):
    digest, record = world.seal(1)
    base = world.versions.get(digest).base_digest
    refuse = lambda **kw: world.activity.entry_refusal(**{"deployment_digest": base, "version_digest": digest,
                                                          "source_digest": record["strategy_digest"], **kw})
    assert refuse() is None
    assert refuse(version_digest=None) == "DEPLOYMENT_VERSION_REQUIRED"
    assert refuse(deployment_digest="sha256:" + "e" * 64) == "DEPLOYMENT_NOT_ACTIVE"
    assert refuse(version_digest="sha256:" + "e" * 64) == "DEPLOYMENT_NOT_ACTIVE"
    assert refuse(source_digest="sha256:" + "f" * 64) == "STRATEGY_SOURCE_MISMATCH"
    world.cooldowns.keys.add("strategies/opening_range_breakout.py:OpeningRangeBreakout")
    assert refuse() == "FAMILY_COOLING_DOWN"
    world.cooldowns.keys.clear()
    world.now = dt.datetime(2026, 11, 9, 15, 0, tzinfo=UTC)
    assert refuse() == "DEPLOYMENT_EXPIRED"
```

- [ ] **Step 3: Run** → fails (`ModuleNotFoundError`).
- [ ] **Step 4: Implement** `trader/automation/ai_deployment_activity.py`:

```python
"""Which deployment versions may enter now (SP2c spec 5.2 items 5 and 8).

Status is computed on every read from the sealed versions, the withdrawals,
the judgments (Plan 1), the XNYS calendar and the cap. Nothing is cached.
"""
from __future__ import annotations

import datetime as dt
import hmac
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Optional
from zoneinfo import ZoneInfo

from trader.automation.ai_deployments import STRATEGY_KIND, AiDeployment, DeploymentRefused
from trader.automation.ai_judgment_port import strategy_key

NEW_YORK = ZoneInfo("America/New_York")
ACTIVE, NOT_STARTED, EXPIRED = "ACTIVE", "NOT_STARTED", "EXPIRED"
WITHDRAWN, SUPERSEDED, JUDGMENT_ENDED, OVER_CAP = "WITHDRAWN", "SUPERSEDED", "JUDGMENT_ENDED", "OVER_CAP"
WIRE_STATE = {ACTIVE: "ACTIVE", NOT_STARTED: "ACTIVE", OVER_CAP: "ACTIVE", EXPIRED: "EXPIRED",
              WITHDRAWN: "WITHDRAWN", SUPERSEDED: "ENDED", JUDGMENT_ENDED: "ENDED"}


def ny_date(moment: dt.datetime) -> dt.date:
    if not isinstance(moment, dt.datetime) or moment.utcoffset() is None:
        raise ValueError("an aware datetime is required")
    return moment.astimezone(NEW_YORK).date()


def deployment_sessions(calendar: Any, *, registered_at: dt.datetime, sessions: int,
                        bundle_expires_at: dt.datetime) -> tuple[dt.date, dt.date]:
    """Ruling 5: from the first session after the registration day, ``sessions`` sessions inclusive,
    never past the last session before the bundle's New York expiry date."""
    start = ny_date(registered_at) + dt.timedelta(days=1)
    days = calendar.sessions_in_range(start, start + dt.timedelta(days=sessions * 2 + 14))
    if len(days) < sessions:
        raise ValueError(f"the calendar has fewer than {sessions} sessions after {start}")
    last_allowed = ny_date(bundle_expires_at) - dt.timedelta(days=1)
    allowed = [day for day in days[:sessions] if day <= last_allowed]
    if not allowed:
        raise DeploymentRefused("BUNDLE_EXPIRED", f"the bundle expires before the first session {days[0]}")
    return days[0], allowed[-1]


def classify(sealed, *, withdrawn: frozenset[str], stands: Mapping[str, bool], today: dt.date,
             max_active: int, unknown_stands: bool = False) -> dict[str, str]:
    """Ruling 6. ``unknown_stands`` decides a version whose judgment was not read yet (registration counts
    it as standing, so a race can only refuse, never exceed the cap)."""
    renewed = {s.version.prior_version for s in sealed if s.version.prior_version is not None}
    status: dict[str, str] = {}
    standing = []
    for s in sealed:
        if s.digest in withdrawn:
            status[s.digest] = WITHDRAWN
        elif s.digest in renewed:
            status[s.digest] = SUPERSEDED
        elif not stands.get(s.digest, unknown_stands):
            status[s.digest] = JUDGMENT_ENDED
        elif today > s.version.expiry_session:
            status[s.digest] = EXPIRED
        else:
            standing.append(s)
    standing.sort(key=lambda s: (s.version.first_session, s.sealed_at, s.digest))
    for rank, s in enumerate(standing):
        if rank >= max_active:
            status[s.digest] = OVER_CAP
        else:
            status[s.digest] = ACTIVE if today >= s.version.first_session else NOT_STARTED
    return status


@dataclass(frozen=True)
class ActiveDeployment:
    version_digest: str
    version: Any            # DeploymentVersion
    deployment: AiDeployment


class DeploymentActivity:
    def __init__(self, *, versions: Any, deployments: Any, judgments: Any, cooldowns: Any, max_active: int,
                 now: Callable[[], dt.datetime]):
        if type(max_active) is not int or max_active < 0:
            raise ValueError("max_active must be an integer >= 0")
        self._versions, self._deployments = versions, deployments
        self._judgments, self._cooldowns = judgments, cooldowns
        self._max_active, self._now = max_active, now

    @property
    def max_active(self) -> int:
        return self._max_active

    def stands_for(self, sealed) -> dict[str, bool]:
        """Read outside any journal transaction: the judgment store has its own lock."""
        result = {}
        for s in sealed:
            facts = self._judgments.get(s.version.judgment_id)
            result[s.digest] = (facts is not None and facts.verdict == "DEPLOY"
                                and all(v == "DEPLOY" for v in self._judgments.renewal_verdicts(s.digest)))
        return result

    def statuses(self) -> dict[str, str]:
        sealed = self._versions.sealed()
        return classify(sealed, withdrawn=self._versions.withdrawn(), stands=self.stands_for(sealed),
                        today=ny_date(self._now()), max_active=self._max_active)

    def status(self, digest: str) -> str:
        found = self.statuses().get(digest)
        if found is None:
            raise DeploymentRefused("DEPLOYMENT_VERSION_UNKNOWN", "no sealed version has this digest")
        return found

    def active(self) -> tuple[ActiveDeployment, ...]:
        chosen = []
        for digest, status in sorted(self.statuses().items()):
            if status == ACTIVE:
                version = self._versions.get(digest)
                chosen.append(ActiveDeployment(digest, version, self._deployments.get_sealed(version.base_digest)))
        return tuple(chosen)

    def entry_refusal(self, *, deployment_digest: str, version_digest: Optional[str],
                      source_digest: Optional[str]) -> Optional[str]:
        if version_digest is None or source_digest is None:
            return "DEPLOYMENT_VERSION_REQUIRED"
        try:
            version = self._versions.get(version_digest)
            status = self.status(version_digest)
        except DeploymentRefused:
            return "DEPLOYMENT_NOT_ACTIVE"
        if version.base_digest != deployment_digest:
            return "DEPLOYMENT_NOT_ACTIVE"
        if status == EXPIRED:
            return "DEPLOYMENT_EXPIRED"
        if status != ACTIVE:
            return "DEPLOYMENT_NOT_ACTIVE"
        deployment = self._deployments.get_sealed(deployment_digest)
        if self._cooldowns.cooling_down(strategy_key(deployment.strategy_path, deployment.class_name), self._now()):
            return "FAMILY_COOLING_DOWN"
        if not hmac.compare_digest(source_digest, deployment.strategy_digest):
            return "STRATEGY_SOURCE_MISMATCH"
        return None


def deployment_version_gate(*, kind_of: Callable[[str], str], activity: DeploymentActivity):
    """Spec 5.2 item 8 at final dispatch, inside the saga's entry lock: journal reads only, no IB."""
    from trader.automation.ai_paper_evidence import AI_PAPER_ACTION

    def gate(request: Any, approval: Any, quote: Any, now: dt.datetime) -> Optional[str]:
        body = getattr(request, "body", None) or {}
        if getattr(request, "action", None) != AI_PAPER_ACTION or body.get("action") != "ENTER":
            return None
        digest = body.get("deployment_digest")
        if digest is None or kind_of(digest) != STRATEGY_KIND:
            return None                       # discretionary: the scope gate owns it
        return activity.entry_refusal(deployment_digest=digest, version_digest=body.get("deployment_version"),
                                      source_digest=body.get("source_digest"))
    return gate
```

- [ ] **Step 5: Run** `tests/automation/test_ai_deployment_activity.py` → pass. **Commit** `feat: compute ai deployment version status, cap and entry refusal`.

### Task 4: Bundle verification for registration

**Files:**
- Create: `trader/automation/ai_bundle_check.py`, `tests/automation/judged_bundle.py`
- Modify: `trader/automation/strategy_binding.py` (rename `_same_values` → `same_values`, `_bar_size_key` → `bar_size_key`; update callers in the same file)
- Test: `tests/automation/test_ai_bundle_check.py`

**Interfaces:**
- Consumes: `ArtifactVerifier.verify`, `require_qualified_research_evidence`, `load_verify_key`, `bundle_dir_name`.
- Produces: `BundleRefused(code, message)`; `BundleFacts(bundle_digest, artifact_id, family_id, strategy_path, class_name, file_hash, params, conids, bar_size, order_notional, reviewer, reviewer_kind, expires_at)`; `ResearchBundleCheck(*, artifacts_root, verify_dir)` with `manifest_artifact_id(bundle_digest) -> str` and `check(bundle_digest, *, artifact_id, now) -> BundleFacts`. Test helper `export_judged_bundle(repo, duckdb_path, *, judgment_id, model_id="openrouter/jev") -> JudgedBundle(bundle_path, bundle_digest, artifact_id, verify_dir, research_db, spec)`.

- [ ] **Step 1: Write the helper** `tests/automation/judged_bundle.py`: `export_judged_bundle` runs `evaluate_synthetic(repo, duckdb_path)` (holdout subset ruleset), records one `OperatorReview` for the result's artifact and decision with `reviewer=f"{model_id}#{judgment_id}"`, `reviewer_kind="llm"`, `reviewed_at=FIXED_NOW`, `holdout_opened_once_confirmed=True` and eight short narrative strings, signs with `AttestationSigner.generate()` through `attest_and_export(..., artifacts_root=repo / "artifacts", now=FIXED_NOW, ruleset=holdout_ruleset())`, writes `signer.public_key_pem()` to `repo / "verify" / "research.pem"`, and returns `JudgedBundle(bundle_path, "sha256:" + bundle_path.name.removeprefix("sha256_"), artifact_id, verify_dir, research_db, spec)`.

- [ ] **Step 2: Write the failing tests**

```python
"""SP2c Plan 2 Task 4: the trader verifies a bundle with public keys only (spec 5.2 item 4, 9 tampering)."""
from __future__ import annotations

import datetime as dt
import json
import os
import shutil

import pytest

from tests.automation.judged_bundle import export_judged_bundle
from tests.research.evaluation_fixtures import FIXED_NOW, judge_qualified_evidence_by_holdout_ruleset
from trader.automation.ai_bundle_check import BundleRefused, ResearchBundleCheck
from trader.research.signing import AttestationSigner

LATER = FIXED_NOW + dt.timedelta(days=1)


@pytest.fixture(scope="module")
def bundle(tmp_path_factory):
    repo = tmp_path_factory.mktemp("judged")
    return export_judged_bundle(repo, str(repo / "market.duckdb"), judgment_id="jdg-1")


@pytest.fixture
def qualified(monkeypatch):
    judge_qualified_evidence_by_holdout_ruleset(monkeypatch)


def check(bundle, root=None, verify=None):
    return ResearchBundleCheck(artifacts_root=root or bundle.bundle_path.parent, verify_dir=verify or bundle.verify_dir)


@pytest.mark.timeout(240)
def test_a_verified_bundle_gives_its_binding(bundle, qualified):
    facts = check(bundle).check(bundle.bundle_digest, artifact_id=bundle.artifact_id, now=LATER)
    assert facts.reviewer == "openrouter/jev#jdg-1" and facts.reviewer_kind == "llm"
    assert facts.conids == tuple(sorted(bundle.spec.conids)) and facts.bar_size == "15 mins"
    assert facts.file_hash.startswith("sha256:") and facts.strategy_path == "strategies/time_of_day.py"
    assert check(bundle).manifest_artifact_id(bundle.bundle_digest) == bundle.artifact_id


def _copy(bundle, tmp_path):
    root = tmp_path / "artifacts"
    shutil.copytree(bundle.bundle_path, root / bundle.bundle_path.name)
    target = root / bundle.bundle_path.name
    for path in (target, *target.iterdir()):
        os.chmod(path, 0o755 if path.is_dir() else 0o644)
    return root, target


@pytest.mark.timeout(240)
@pytest.mark.parametrize("case,code", [("params", "BUNDLE_INVALID"), ("expired", "BUNDLE_EXPIRED"),
                                       ("unknown_key", "BUNDLE_INVALID"), ("missing", "BUNDLE_MISSING"),
                                       ("no_keys", "BUNDLE_KEYS_MISSING"), ("unqualified", "BUNDLE_NOT_QUALIFIED")])
def test_tampered_expired_or_unknown_key_bundles_are_refused(bundle, qualified, tmp_path, case, code, monkeypatch):
    root, target = _copy(bundle, tmp_path)
    verify, now = bundle.verify_dir, LATER
    if case == "params":
        artifact = json.loads((target / "artifact.json").read_text())
        artifact["selected_parameters"]["ENTRY_MINUTE"] = 601
        (target / "artifact.json").write_text(json.dumps(artifact))
    elif case == "expired":
        now = FIXED_NOW + dt.timedelta(days=91)
    elif case == "unknown_key":
        verify = tmp_path / "other"
        verify.mkdir()
        (verify / "x.pem").write_bytes(AttestationSigner.generate().public_key_pem())
    elif case == "missing":
        shutil.rmtree(target)
    elif case == "no_keys":
        verify = tmp_path / "empty"
        verify.mkdir()
    elif case == "unqualified":
        monkeypatch.undo()                      # the full paper-v1 rule set: the synthetic bundle fails it
    with pytest.raises(BundleRefused) as refused:
        check(bundle, root, verify).check(bundle.bundle_digest, artifact_id=bundle.artifact_id, now=now)
    assert refused.value.code == code
```

- [ ] **Step 3: Run** `tests/automation/test_ai_bundle_check.py` → fails (`ModuleNotFoundError`).
- [ ] **Step 4: Implement** `trader/automation/ai_bundle_check.py`:

```python
"""Verify a research bundle for a judgment-bound registration (SP2c spec 5.2 item 4).

Public keys only. The trader reads the bundle and never writes into the artifacts directory.
"""
from __future__ import annotations

import datetime as dt
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Optional

from trader.automation.artifact_verifier import ArtifactExpired, ArtifactVerifier, ArtifactVerifierError
from trader.automation.paper_materials import PaperMaterialsError, require_qualified_research_evidence
from trader.research.attest_export import bundle_dir_name
from trader.research.signing import InvalidKeyType, MalformedKey, load_verify_key

BUNDLE_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
DEFAULT_ARTIFACTS_ROOT = "~/.local/share/mmr/artifacts"
DEFAULT_VERIFY_DIR = "~/.config/mmr/keys/verify"


class BundleRefused(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(f"{code}: {message}")
        self.code, self.message = code, message


@dataclass(frozen=True)
class BundleFacts:
    bundle_digest: str
    artifact_id: str
    family_id: str
    strategy_path: str
    class_name: str
    file_hash: str
    params: Mapping[str, Any]
    conids: tuple[int, ...]
    bar_size: Optional[str]
    order_notional: Optional[float]
    reviewer: str
    reviewer_kind: str
    expires_at: dt.datetime


def _conids(instruments) -> tuple[int, ...]:
    values = []
    for text in instruments:
        if not isinstance(text, str) or not text.isdigit() or text.startswith("0"):
            raise BundleRefused("BUNDLE_INVALID", f"attested instrument {text!r} is not a conid")
        values.append(int(text))
    return tuple(sorted(values))


class ResearchBundleCheck:
    def __init__(self, *, artifacts_root: Path, verify_dir: Path):
        self._root = Path(artifacts_root).expanduser()
        self._verify_dir = Path(verify_dir).expanduser()

    def _path(self, bundle_digest: str) -> Path:
        if not isinstance(bundle_digest, str) or not BUNDLE_DIGEST.match(bundle_digest):
            raise BundleRefused("BUNDLE_INVALID", "bundle_digest must be sha256:<64 hex>")
        return self._root / bundle_dir_name(bundle_digest)

    def _keys(self) -> list:
        paths = sorted(self._verify_dir.glob("*.pem"))
        if not paths:
            raise BundleRefused("BUNDLE_KEYS_MISSING", f"no public key in {self._verify_dir}")
        try:
            return [load_verify_key(str(path)) for path in paths]
        except (InvalidKeyType, MalformedKey, OSError) as ex:
            raise BundleRefused("BUNDLE_KEYS_MISSING", f"a verify key is unusable: {type(ex).__name__}") from None

    def manifest_artifact_id(self, bundle_digest: str) -> str:
        """The artifact the bundle names, read before verification only to choose JUDGMENT_MISMATCH."""
        try:
            return str(json.loads((self._path(bundle_digest) / "manifest.json").read_text())["artifact_id"])
        except (OSError, ValueError, KeyError, TypeError):
            raise BundleRefused("BUNDLE_MISSING", "no readable bundle manifest for this digest") from None

    def check(self, bundle_digest: str, *, artifact_id: str, now: dt.datetime) -> BundleFacts:
        path = self._path(bundle_digest)
        if not path.is_dir():
            raise BundleRefused("BUNDLE_MISSING", f"no bundle directory {path.name}")
        try:
            verified = ArtifactVerifier(self._keys()).verify(path, "paper", artifact_id, now)
        except ArtifactExpired as ex:
            raise BundleRefused("BUNDLE_EXPIRED", str(ex)) from None
        except (ArtifactVerifierError, OSError, ValueError) as ex:
            raise BundleRefused("BUNDLE_INVALID", str(ex)) from None
        try:
            require_qualified_research_evidence(path)
        except PaperMaterialsError as ex:
            raise BundleRefused("BUNDLE_NOT_QUALIFIED", str(ex)) from None
        if "sha256:" + verified.manifest_digest != bundle_digest:
            raise BundleRefused("BUNDLE_INVALID", "the directory does not hold the bundle its name claims")
        artifact = json.loads((path / "artifact.json").read_text())     # checksum-verified above
        review = json.loads((path / "review.json").read_text())
        attested = verified.attested_strategy
        return BundleFacts(
            bundle_digest=bundle_digest, artifact_id=verified.artifact_id, family_id=artifact["family_id"],
            strategy_path=attested.strategy_path, class_name=attested.class_name,
            file_hash="sha256:" + attested.source_digest, params=dict(verified.parameters),
            conids=_conids(verified.allowlist), bar_size=attested.bar_size, order_notional=attested.order_notional,
            reviewer=review["reviewer"], reviewer_kind=review["reviewer_kind"], expires_at=verified.expires_at)
```

- [ ] **Step 5: Run** `tests/automation/test_ai_bundle_check.py tests/test_strategy_binding*.py tests/test_strategy_artifact_soft_load.py` → pass. **Commit** `feat: verify research bundles for ai deployment registration`.

### Task 5: Registrar, withdraw and views

**Files:**
- Create: `trader/automation/ai_deployment_registration.py`
- Modify: `trader/automation/ai_paper_actions.py`
- Test: `tests/automation/test_ai_deployment_registration.py`

**Interfaces:**
- Consumes: Tasks 1–4.
- Produces: `registration_command_id(body, day: date) -> str`; `request_digest(body) -> str`; `binding_differences(deployment, bundle, cases, *, bundle_digest, initial_judgment_id) -> list[str]`; `AiDeploymentRegistrar(*, db, deployments, versions, activity, judgments, cooldowns, bundles, calendar, expiry_sessions, now)` with `register(body, *, principal, command_id) -> dict`; `AiPaperActions(..., registrar, versions, activity)` gains `withdraw(cmd)`, `version_view(digest)`, `active_view()`, `registration_command_id(body)`; `WITHDRAW_ACTION = "withdraw_ai_deployment"`.

- [ ] **Step 1: Write the failing tests** (fake bundle check; real stores, calendar and Task 3 helpers)

```python
"""SP2c Plan 2 Task 5: registration bound to a DEPLOY judgment (spec 5.2 items 4-5, 9)."""
from __future__ import annotations

import datetime as dt
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

import pytest

from tests.automation.judged_deployment import Cooldowns, SeededJudgments, deploy_facts
from trader.automation.ai_bundle_check import BundleFacts, BundleRefused
from trader.automation.ai_deployment_activity import SUPERSEDED, DeploymentActivity
from trader.automation.ai_deployment_registration import AiDeploymentRegistrar, registration_command_id
from trader.automation.ai_deployment_versions import AiDeploymentVersionStore, apply_ai_deployment_version_migrations
from trader.automation.ai_deployments import (
    AiDeployment, AiDeploymentStore, DeploymentRefused, apply_ai_deployment_migration,
)
from trader.automation.calendar_policy import XNYSCalendarPolicy
from trader.data.duckdb_store import DuckDBConnection
from trader.data.schema_migrations import SchemaMigrator

UTC = dt.timezone.utc
NOW = dt.datetime(2026, 10, 9, 21, 0, tzinfo=UTC)


def record(n=0, **changes):
    return {"strategy_path": "strategies/opening_range_breakout.py", "strategy_digest": "sha256:" + "a" * 64,
            "class_name": "OpeningRangeBreakout", "params": {"RANGE_MINUTES": 15}, "conids": [265598 + n, 272093],
            "bar_size": "1 min", "style": "intraday_long", "decider": "jev", "decider_verdict": "DEPLOY",
            "evidence_ref": f"sha256:{n + 1:064x}", "evidence_order_notional": 2000.0, **changes}


class FakeBundles:
    def __init__(self):
        self.facts: dict[str, BundleFacts] = {}

    def manifest_artifact_id(self, digest):
        return self.facts[digest].artifact_id

    def check(self, digest, *, artifact_id, now):
        facts = self.facts.get(digest)
        if facts is None:
            raise BundleRefused("BUNDLE_MISSING", digest)
        if now >= facts.expires_at:
            raise BundleRefused("BUNDLE_EXPIRED", digest)
        return facts


class Env:
    def __init__(self, tmp_path, max_active=3):
        self.now = NOW
        self.db = DuckDBConnection.get_instance(str(tmp_path / "journal.duckdb"))
        migrator = SchemaMigrator(self.db)
        apply_ai_deployment_migration(migrator)
        apply_ai_deployment_version_migrations(migrator)
        clock = lambda: self.now
        self.deployments = AiDeploymentStore(self.db, now=clock)
        self.versions = AiDeploymentVersionStore(self.db, now=clock)
        self.judgments, self.cooldowns, self.bundles = SeededJudgments(), Cooldowns(), FakeBundles()
        self.activity = DeploymentActivity(versions=self.versions, deployments=self.deployments,
                                           judgments=self.judgments, cooldowns=self.cooldowns,
                                           max_active=max_active, now=clock)
        self.registrar = AiDeploymentRegistrar(
            db=self.db, deployments=self.deployments, versions=self.versions, activity=self.activity,
            judgments=self.judgments, cooldowns=self.cooldowns, bundles=self.bundles,
            calendar=XNYSCalendarPolicy(), expiry_sessions=20, now=clock)

    def judge(self, judgment_id, rec, *, initial=None, expires_at=NOW + dt.timedelta(days=90), **kw):
        self.judgments.seed(deploy_facts(rec, judgment_id, **kw))
        self.bundles.facts[rec["evidence_ref"]] = BundleFacts(
            rec["evidence_ref"], "art-1", "fam-1", rec["strategy_path"], rec["class_name"], rec["strategy_digest"],
            rec["params"], tuple(sorted(rec["conids"])), rec["bar_size"], rec["evidence_order_notional"],
            f"jev-model#{initial or judgment_id}", "llm", expires_at)

    def register(self, judgment_id, rec):
        body = {"judgment_id": judgment_id, "bundle_digest": rec["evidence_ref"],
                "deployment": AiDeployment.from_json(rec).to_json()}
        return self.registrar.register(body, principal="ai_research",
                                       command_id=registration_command_id(body, self.now.date()))

    def refused(self, judgment_id, rec) -> str:
        with pytest.raises((DeploymentRefused, BundleRefused)) as refused:
            self.register(judgment_id, rec)
        return refused.value.code


@pytest.fixture
def env(tmp_path):
    return Env(tmp_path)


def test_registration_refusals_bind_the_judgment(env):
    assert env.refused("jdg-none", record()) == "JUDGMENT_MISSING"
    env.judge("jdg-rej", record(), verdict="REJECT")
    assert env.refused("jdg-rej", record()) == "JUDGMENT_NOT_DEPLOY"
    env.judge("jdg-1", record())
    for change in ({"params": {"RANGE_MINUTES": 16}}, {"conids": [265598, 999999]}, {"bar_size": "5 mins"},
                   {"strategy_digest": "sha256:" + "f" * 64}, {"evidence_order_notional": 1000.0}):
        assert env.refused("jdg-1", record(**change)) == "JUDGMENT_MISMATCH"
    env.judgments.seed(deploy_facts(record(), "jdg-1", artifact_id="art-other"))    # another evaluation
    assert env.refused("jdg-1", record()) == "JUDGMENT_MISMATCH"
    env.judge("jdg-2", record(), model_id="jev-model", initial="jdg-someone-else")   # review names another
    assert env.refused("jdg-2", record()) == "JUDGMENT_MISMATCH"


def test_exact_retry_returns_the_same_versions(env):
    env.judge("jdg-1", record())
    first = env.register("jdg-1", record())
    assert (first["first_session"], first["expiry_session"], first["created"]) == ("2026-10-12", "2026-11-06", True)
    env.now = NOW + dt.timedelta(days=3)                                             # a later day: still the same
    again = env.register("jdg-1", record())
    assert (again["digest"], again["version_digest"], again["created"]) == (first["digest"], first["version_digest"],
                                                                             False)


def test_another_body_for_a_bound_judgment_is_refused(env):
    env.judge("jdg-1", record())
    env.register("jdg-1", record())
    env.bundles.facts[record(5)["evidence_ref"]] = replace(env.bundles.facts[record()["evidence_ref"]],
                                                           bundle_digest=record(5)["evidence_ref"])
    assert env.refused("jdg-1", record(evidence_ref=record(5)["evidence_ref"])) == "JUDGMENT_ALREADY_BOUND"


def test_cooldown_and_cap(env):
    env.cooldowns.keys.add("strategies/opening_range_breakout.py:OpeningRangeBreakout")
    env.judge("jdg-1", record())
    assert env.refused("jdg-1", record()) == "FAMILY_COOLING_DOWN"
    env.cooldowns.keys.clear()
    for n in range(3):
        env.judge(f"jdg-c{n}", record(n))
        env.register(f"jdg-c{n}", record(n))
    env.judge("jdg-c3", record(3))
    assert env.refused("jdg-c3", record(3)) == "DEPLOY_CAP_REACHED"


def test_two_concurrent_registrations_for_the_last_slot(tmp_path):
    env = Env(tmp_path, max_active=1)
    for n in (0, 1):
        env.judge(f"jdg-{n}", record(n))

    def attempt(n):
        try:
            return env.register(f"jdg-{n}", record(n))["version_digest"]
        except DeploymentRefused as ex:
            return ex.code
    with ThreadPoolExecutor(2) as pool:
        results = sorted(pool.map(attempt, (0, 1)))
    assert results.count("DEPLOY_CAP_REACHED") == 1 and len(env.versions.sealed()) == 1


def renew(env, prior_version, judgment_id="jdg-r1"):
    env.judgments.seed(deploy_facts(record(), judgment_id, kind="RENEWAL", renews=prior_version))
    return env.register(judgment_id, record())


def test_renewal_gets_a_fresh_version_and_supersedes(env):
    env.judge("jdg-1", record())
    initial = env.register("jdg-1", record())
    env.now = dt.datetime(2026, 11, 6, 21, 0, tzinfo=UTC)                           # evening of the last session
    renewal = renew(env, initial["version_digest"])
    assert renewal["digest"] == initial["digest"] and renewal["version_digest"] != initial["version_digest"]
    assert (renewal["kind"], renewal["first_session"]) == ("RENEWAL", "2026-11-09")
    assert env.activity.status(initial["version_digest"]) == SUPERSEDED


def test_the_same_renewal_twice_returns_the_same_version(env):
    env.judge("jdg-1", record())
    initial = env.register("jdg-1", record())
    first = renew(env, initial["version_digest"])
    again = renew(env, initial["version_digest"])
    assert (again["version_digest"], first["created"], again["created"]) == (first["version_digest"], True, False)


def test_renewal_is_refused_after_the_bundle_expires(env):
    env.judge("jdg-1", record(), expires_at=dt.datetime(2026, 11, 20, tzinfo=UTC))
    initial = env.register("jdg-1", record())
    env.now = dt.datetime(2026, 11, 20, 21, 0, tzinfo=UTC)
    with pytest.raises(BundleRefused) as refused:
        renew(env, initial["version_digest"])
    assert refused.value.code == "BUNDLE_EXPIRED"


def test_a_withdrawn_prior_cannot_be_renewed(env):
    env.judge("jdg-1", record())
    initial = env.register("jdg-1", record())
    env.versions.withdraw(initial["version_digest"], reason="x", principal="cli", command_id="w")
    with pytest.raises(DeploymentRefused) as refused:
        renew(env, initial["version_digest"])
    assert refused.value.code == "RENEWAL_PRIOR_INVALID"
```

Add one real-chain test in the same file (module fixture from Task 4):

```python
@pytest.mark.timeout(240)
def test_registration_through_a_real_signed_bundle(tmp_path_factory, monkeypatch):
    from tests.automation.judged_bundle import export_judged_bundle
    from tests.research.evaluation_fixtures import FIXED_NOW, judge_qualified_evidence_by_holdout_ruleset
    from trader.automation.ai_bundle_check import ResearchBundleCheck
    judge_qualified_evidence_by_holdout_ruleset(monkeypatch)
    repo = tmp_path_factory.mktemp("chain")
    bundle = export_judged_bundle(repo, str(repo / "market.duckdb"), judgment_id="jdg-chain",
                                  model_id="openrouter/jev")
    env = Env(repo)
    env.now = FIXED_NOW + dt.timedelta(days=1)
    env.registrar._bundles = ResearchBundleCheck(artifacts_root=repo / "artifacts", verify_dir=bundle.verify_dir)
    facts = env.registrar._bundles.check(bundle.bundle_digest, artifact_id=bundle.artifact_id, now=env.now)
    rec = {"strategy_path": facts.strategy_path, "strategy_digest": facts.file_hash, "class_name": facts.class_name,
           "params": dict(facts.params), "conids": list(facts.conids), "bar_size": facts.bar_size,
           "style": "intraday_long", "decider": "jev", "decider_verdict": "DEPLOY",
           "evidence_ref": bundle.bundle_digest, "evidence_order_notional": facts.order_notional}
    env.judgments.seed(deploy_facts(rec, "jdg-chain", model_id="openrouter/jev", artifact_id=bundle.artifact_id,
                                    family_id=facts.family_id))
    assert env.register("jdg-chain", rec)["created"] is True
    assert env.refused("jdg-chain", {**rec, "params": {**rec["params"], "ENTRY_MINUTE": 615}}) == \
        "JUDGMENT_ALREADY_BOUND"
```

- [ ] **Step 2: Run** → fails (`ModuleNotFoundError`).
- [ ] **Step 3: Implement** `trader/automation/ai_deployment_registration.py`:

```python
"""``register_ai_deployment`` bound to a durable DEPLOY judgment and a verified bundle (SP2c spec 5.2 item 4)."""
from __future__ import annotations

import datetime as dt
import hashlib
import re
from typing import Any, Callable, Mapping, Optional, Sequence

from trader.automation.ai_bundle_check import BundleFacts, BundleRefused
from trader.automation.ai_deployment_activity import (
    ACTIVE, NOT_STARTED, classify, deployment_sessions, ny_date,
)
from trader.automation.ai_deployment_versions import INITIAL, DeploymentVersion
from trader.automation.ai_deployments import (
    STRATEGY_DIGEST_PROVENANCE, AiDeployment, DeploymentRefused, deployment_digest,
)
from trader.automation.ai_judgment_port import JudgmentFacts, strategy_key
from trader.automation.strategy_binding import bar_size_key, same_values
from trader.research.canonical import canonical_json_bytes
from trader.research.strategy_paths import normalize_strategy_path

KEYS = frozenset({"judgment_id", "bundle_digest", "deployment"})
COMMAND_PREFIX = "aidep-"
_JUDGMENT_ID = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")


def registration_command_id(body: Mapping[str, Any], day: dt.date) -> str:
    """Ruling 1: a same-day retry replays the ledger; another body or day reaches the handler."""
    material = canonical_json_bytes(dict(body)) + b"|" + day.isoformat().encode()
    return COMMAND_PREFIX + hashlib.sha256(material).hexdigest()[:48]


def request_digest(body: Mapping[str, Any]) -> str:
    return "sha256:" + hashlib.sha256(canonical_json_bytes(dict(body))).hexdigest()


def binding_differences(deployment: AiDeployment, bundle: BundleFacts, cases: Sequence[JudgmentFacts], *,
                        bundle_digest: str, initial_judgment_id: str) -> list[str]:
    """Rulings 3-4: every field the bundle, each judgment's case and the body must agree on."""
    problems: list[str] = []

    def same(name: str, *values: Any) -> None:
        if any(value != values[0] for value in values[1:]):
            problems.append(f"{name} differs: {list(values)!r}")
    same("strategy_path", *(normalize_strategy_path(p) for p in
                            (deployment.strategy_path, bundle.strategy_path, *(c.strategy_path for c in cases))))
    same("class_name", deployment.class_name, bundle.class_name, *(c.class_name for c in cases))
    same("file_hash", deployment.strategy_digest, bundle.file_hash, *(c.file_hash for c in cases))
    if not all(same_values(dict(deployment.params), dict(p)) for p in (bundle.params, *(c.params for c in cases))):
        problems.append("params differ")
    same("conids", tuple(deployment.conids), bundle.conids, *(tuple(sorted(c.conids)) for c in cases))
    same("bar_size", *(bar_size_key(b) for b in (deployment.bar_size, bundle.bar_size or "",
                                                 *(c.bar_size for c in cases))))
    same("evidence_ref", deployment.evidence_ref, bundle_digest)
    same("order_notional", deployment.evidence_order_notional, bundle.order_notional)
    for case in cases:
        if case.artifact_id is not None:
            same("artifact_id", case.artifact_id, bundle.artifact_id)
            same("family_id", case.family_id, bundle.family_id)
    model, _, reviewed_id = bundle.reviewer.rpartition("#")
    if bundle.reviewer_kind != "llm" or not model or reviewed_id != initial_judgment_id:
        problems.append(f"the bundle review {bundle.reviewer!r} does not name judgment {initial_judgment_id}")
    if deployment.decider_verdict != "DEPLOY":
        problems.append("decider_verdict must be DEPLOY")
    return problems


class AiDeploymentRegistrar:
    def __init__(self, *, db: Any, deployments: Any, versions: Any, activity: Any, judgments: Any, cooldowns: Any,
                 bundles: Any, calendar: Any, expiry_sessions: int, now: Callable[[], dt.datetime]):
        self._db, self._deployments, self._versions, self._activity = db, deployments, versions, activity
        self._judgments, self._cooldowns, self._bundles = judgments, cooldowns, bundles
        self._calendar, self._expiry_sessions, self._now = calendar, expiry_sessions, now

    def register(self, body: Mapping[str, Any], *, principal: str, command_id: str) -> dict:
        judgment_id, bundle_digest, deployment = self._parse(body)
        digest_of_request = request_digest(body)
        judgment = self._deploy_judgment(judgment_id)
        bound = self._versions.bound_to_judgment(judgment_id)
        if bound is not None:                       # exact retry on a later day, or another body
            if bound.request_digest != digest_of_request:
                raise DeploymentRefused("JUDGMENT_ALREADY_BOUND", f"judgment {judgment_id} has a version")
            return self._outcome(bound.digest, bound.version, created=False)
        prior, initial = self._line(judgment, deployment)
        now = self._now()
        if self._bundles.manifest_artifact_id(bundle_digest) != initial.artifact_id:
            raise DeploymentRefused("JUDGMENT_MISMATCH", "the bundle names another evaluation's artifact")
        bundle = self._bundles.check(bundle_digest, artifact_id=initial.artifact_id, now=now)
        cases = (initial,) if prior is None else (initial, judgment)
        problems = binding_differences(deployment, bundle, cases, bundle_digest=bundle_digest,
                                       initial_judgment_id=initial.judgment_id)
        if problems:
            raise DeploymentRefused("JUDGMENT_MISMATCH", "; ".join(problems))
        if self._cooldowns.cooling_down(strategy_key(deployment.strategy_path, deployment.class_name), now):
            raise DeploymentRefused("FAMILY_COOLING_DOWN", "the strategy key is cooling down")
        first, expiry = deployment_sessions(self._calendar, registered_at=now, sessions=self._expiry_sessions,
                                            bundle_expires_at=bundle.expires_at)
        version = DeploymentVersion(base_digest=deployment_digest(deployment), judgment_id=judgment_id,
                                    kind=judgment.kind, prior_version=prior, first_session=first,
                                    expiry_session=expiry)
        stands = self._activity.stands_for(self._versions.sealed())   # before the transaction (lock)

        def write(conn) -> tuple[str, bool]:
            self._require_room_in_tx(conn, stands, today=ny_date(now), excluding=prior)
            if prior is not None and prior in self._versions.withdrawn_in_tx(conn):
                raise DeploymentRefused("RENEWAL_PRIOR_INVALID", "the prior version was withdrawn")
            self._deployments.register_in_tx(conn, deployment, principal=principal, command_id=command_id)
            return self._versions.seal_in_tx(conn, version, request_digest=digest_of_request,
                                             principal=principal, command_id=command_id)
        digest, created = self._db.transaction(write)
        return self._outcome(digest, version, created=created)

    @staticmethod
    def _parse(body: Mapping[str, Any]) -> tuple[str, str, AiDeployment]:
        if not isinstance(body, Mapping) or set(body) != KEYS:
            raise DeploymentRefused("DEPLOYMENT_INVALID", f"registration has exactly the keys {sorted(KEYS)}")
        if not isinstance(body["judgment_id"], str) or not _JUDGMENT_ID.match(body["judgment_id"]):
            raise DeploymentRefused("DEPLOYMENT_INVALID", "judgment_id must match ^[A-Za-z0-9_.:-]{1,128}$")
        return body["judgment_id"], body["bundle_digest"], AiDeployment.from_json(body["deployment"])

    def _deploy_judgment(self, judgment_id: str) -> JudgmentFacts:
        judgment = self._judgments.get(judgment_id)
        if judgment is None:
            raise DeploymentRefused("JUDGMENT_MISSING", f"no durable judgment {judgment_id}")
        if judgment.verdict != "DEPLOY":
            raise DeploymentRefused("JUDGMENT_NOT_DEPLOY", f"judgment {judgment_id} is {judgment.verdict}")
        return judgment

    def _line(self, judgment: JudgmentFacts, deployment: AiDeployment) -> tuple[Optional[str], JudgmentFacts]:
        """(prior version digest, the INITIAL judgment of the line) — ruling 7."""
        if judgment.kind == INITIAL:
            return None, judgment
        try:
            prior = self._versions.get(judgment.renews_version)
        except DeploymentRefused:
            raise DeploymentRefused("RENEWAL_PRIOR_INVALID", "the renewal names no sealed version") from None
        if prior.base_digest != deployment_digest(deployment):
            raise DeploymentRefused("JUDGMENT_MISMATCH", "a renewal must keep the prior base deployment")
        first = prior
        while first.prior_version is not None:
            first = self._versions.get(first.prior_version)
        return judgment.renews_version, self._deploy_judgment(first.judgment_id)

    def _require_room_in_tx(self, conn, stands, *, today: dt.date, excluding: Optional[str]) -> None:
        statuses = classify(self._versions.sealed_in_tx(conn), withdrawn=self._versions.withdrawn_in_tx(conn),
                            stands=stands, today=today, max_active=10 ** 6, unknown_stands=True)
        standing = [d for d, s in statuses.items() if s in (ACTIVE, NOT_STARTED) and d != excluding]
        if len(standing) >= self._activity.max_active:
            raise DeploymentRefused("DEPLOY_CAP_REACHED", f"{len(standing)} AI deployments already stand")

    @staticmethod
    def _outcome(digest: str, version: DeploymentVersion, *, created: bool) -> dict:
        return {"digest": version.base_digest, "version_digest": digest, "kind": version.kind,
                "first_session": version.first_session.isoformat(),
                "expiry_session": version.expiry_session.isoformat(), "created": created,
                "strategy_digest_provenance": STRATEGY_DIGEST_PROVENANCE}
```

In `ai_paper_actions.py`: constructor gains `registrar: Any = None, versions: Any = None, activity: Any = None`; keep `now` as `self._now`. Replace `register`:

```python
    def register(self, cmd: CommandRequest) -> dict:
        if cmd.principal != AI_RESEARCH:
            raise CommandValidationError("PRINCIPAL_FORBIDDEN", "only ai_research registers deployments")
        if self._account_mode != "paper" or not str(self._account_id).startswith("DU"):
            raise CommandValidationError("ACCOUNT_NOT_PAPER", "AI deployments are paper only")
        try:
            style = AiDeployment.from_json((cmd.body or {}).get("deployment")).style
            if not self._config.style_enabled(style):
                raise CommandValidationError(STYLE_NOT_ENABLED, f"style {style!r} is not enabled")
            return self._registrar.register(cmd.body, principal=cmd.principal, command_id=cmd.command_id)
        except (DeploymentRefused, BundleRefused) as ex:
            raise CommandValidationError(ex.code, ex.message) from None

    def registration_command_id(self, body: dict) -> str:
        return registration_command_id(body, ny_date(self._now()))

    def withdraw(self, cmd: CommandRequest) -> dict:
        if cmd.principal not in OPERATORS:
            raise CommandValidationError("PRINCIPAL_FORBIDDEN", "only an operator withdraws a deployment")
        try:
            newly = self._versions.withdraw(cmd.body["version_digest"], reason=cmd.body["reason"],
                                            principal=cmd.principal, command_id=cmd.command_id)
        except DeploymentRefused as ex:
            raise CommandValidationError(ex.code, ex.message) from None
        return {"version_digest": cmd.body["version_digest"], "withdrawn": True, "already_withdrawn": not newly}

    def version_view(self, digest: str) -> dict:
        try:
            version, status = self._versions.get(digest), self._activity.status(digest)
        except DeploymentRefused:
            return {"found": False, "version": None}
        return {"found": True, "version": {
            "version_digest": digest, "base_digest": version.base_digest, "judgment_id": version.judgment_id,
            "kind": version.kind, "prior_version_digest": version.prior_version,
            "first_session": version.first_session.isoformat(), "expiry_session": version.expiry_session.isoformat(),
            "state": WIRE_STATE[status]}}

    def active_view(self) -> dict:
        if self._account_mode != "paper":
            return {"account_mode": "paper", "deployments": []}   # never reached: ai_paper is paper only
        return {"account_mode": "paper", "deployments": [
            {"version_digest": a.version_digest, "base_digest": a.version.base_digest,
             "strategy_path": a.deployment.strategy_path, "strategy_digest": a.deployment.strategy_digest,
             "class_name": a.deployment.class_name, "params": dict(a.deployment.params),
             "conids": list(a.deployment.conids), "bar_size": a.deployment.bar_size,
             "expiry_session": a.version.expiry_session.isoformat()} for a in self._activity.active()]}
```

with `OPERATORS = frozenset({"cli", "dashboard"})`, `WITHDRAW_ACTION = "withdraw_ai_deployment"`, and the imports (`BundleRefused`, `WIRE_STATE`, `ny_date`, `registration_command_id`). Update the module docstring to name the new actions.

- [ ] **Step 4: Run** `tests/automation/test_ai_deployment_registration.py` → pass (the real-chain test may take ~3 min).
- [ ] **Step 5: Commit** `feat: bind ai deployment registration to a deploy judgment and a verified bundle`.

### Task 6: RPC surface, ACL and trader wiring

**Files:**
- Create: `trader/messaging/ai_deployment_wire.py`
- Modify: `trader/messaging/production_api.py`, `trader/messaging/principals.py`, `trader/trading/command_stack.py`, `docker-compose.yml` (trader: `${HOME}/.config/mmr/keys/verify:/home/trader/.config/mmr/keys/verify:ro` if absent), `tests/automation/judged_deployment.py` (seeding helpers), `tests/test_rpc_acl.py` (`AI_PAPER_FAMILY`), `tests/test_ai_paper_rpc.py` (new registration body)
- Test: `tests/automation/test_ai_deployment_rpc.py`

**Interfaces:**
- Consumes: Tasks 1–5; Plan 1's `BacktestJudgments` (built here now), `cooling_until_in_tx`, `AiPaperConfig.backtest_judge`, `default_cases_dir` layout (`<artifacts_root>/cases`).
- Produces: the four RPC methods (Cross-plan additions); `_AiPaperParts.versions`, `.activity`; `AiPaperServices.versions`, `.activity`; `trader.ai_deployment_versions`, `trader.ai_deployment_activity`; test helpers `install_seeded_judgments(monkeypatch) -> SeededJudgments` and `seed_judged_deployment(services, judgments, record, *, today, sessions=20, judgment_id="jdg-seed-00000001") -> tuple[str, str]`.

- [ ] **Step 1: Add the seeding helpers** to `tests/automation/judged_deployment.py` (ruling 17):

```python
def install_seeded_judgments(monkeypatch) -> SeededJudgments:
    """Before a stack is built: the trader's judgment reader answers seeded DEPLOY judgments only."""
    import trader.automation.ai_judgment_port as port
    seeded = SeededJudgments()
    monkeypatch.setattr(port, "judgment_reader_for", lambda judgments, **paths: seeded)
    return seeded


def seed_judged_deployment(services, judgments: SeededJudgments, record: dict, *, today, sessions: int = 20,
                           judgment_id: str = "jdg-seed-00000001") -> tuple[str, str]:
    """A base deployment and an ACTIVE version from ``today``, without the bundle path."""
    import datetime as dt
    import hashlib
    from trader.automation.ai_deployment_versions import INITIAL, DeploymentVersion
    from trader.automation.ai_deployments import AiDeployment, deployment_digest
    deployment = AiDeployment.from_json(record)
    judgments.seed(deploy_facts(record, judgment_id))
    version = DeploymentVersion(deployment_digest(deployment), judgment_id, INITIAL, None, today,
                                today + dt.timedelta(days=sessions * 2))
    request = "sha256:" + hashlib.sha256(judgment_id.encode()).hexdigest()

    def write(conn):
        services.deployments.register_in_tx(conn, deployment, principal="ai_research",
                                            command_id=f"seed-{judgment_id}")
        return services.versions.seal_in_tx(conn, version, request_digest=request, principal="ai_research",
                                            command_id=f"seed-{judgment_id}")[0]
    return deployment_digest(deployment), services.versions._db.transaction(write)
```

- [ ] **Step 2: Write the failing tests**

```python
"""SP2c Plan 2 Task 6: the four deployment methods over signed RPC (spec 5.1 table)."""
from __future__ import annotations

import pytest

from tests.automation.judged_deployment import install_seeded_judgments, seed_judged_deployment
from tests.sp1_fixtures import served_stack
from trader.messaging.principals import TRADER_ACL
from trader.messaging.typed_rpc import TypedRpcRemoteError
from trader.acceptance.scenario import AcceptanceSettings, deployment_record


@pytest.fixture
def served(tmp_path, loop_thread, monkeypatch):
    seeded = install_seeded_judgments(monkeypatch)
    stack = served_stack(tmp_path, loop_thread, monkeypatch)
    stack.seeded = seeded
    yield stack
    stack.close()


def test_rights_are_exact():
    assert TRADER_ACL[("query", "get_active_ai_deployments")] == {"strategy"}
    assert TRADER_ACL[("query", "get_ai_deployment_version")] == {"cli", "dashboard", "ai_supervisor", "ai_research"}
    assert TRADER_ACL[("command", "withdraw_ai_deployment")] == {"cli", "dashboard"}


@pytest.mark.parametrize("principal,method,body", [
    ("cli", "get_active_ai_deployments", {}), ("ai_supervisor", "get_active_ai_deployments", {}),
    ("ai_research", "withdraw_ai_deployment", {"version_digest": "sha256:" + "a" * 64, "reason": "x"}),
    ("strategy", "get_ai_deployment_version", {"version_digest": "sha256:" + "a" * 64})])
def test_callers_outside_the_row_are_denied(served, principal, method, body):
    with pytest.raises(TypedRpcRemoteError) as exc:
        served.call(principal, method, body)
    assert exc.value.code == "PERMISSION_DENIED"


def test_strategy_reads_the_active_set_and_an_operator_withdraws(served):
    base, version = seed_judged_deployment(served.composed.stack.ai_paper, served.seeded,
                                           deployment_record(AcceptanceSettings(run_id="r", account_id="DU1",
                                                                                strategy_bytes=b"x")),
                                           today=served.now().date())
    active = served.call("strategy", "get_active_ai_deployments", {})
    assert [d["version_digest"] for d in active["deployments"]] == [version]
    assert served.call("ai_research", "get_ai_deployment_version", {"version_digest": version})["version"][
        "state"] == "ACTIVE"
    receipt = served.call("cli", "withdraw_ai_deployment", {"version_digest": version, "reason": "operator"})
    assert receipt["state"] == "RESOLVED" and receipt["outcome"]["already_withdrawn"] is False
    assert served.call("strategy", "get_active_ai_deployments", {})["deployments"] == []


def test_registration_without_a_judgment_is_refused_on_the_wire(served):
    record = deployment_record(AcceptanceSettings(run_id="r", account_id="DU1", strategy_bytes=b"x"))
    receipt = served.call("ai_research", "register_ai_deployment", {
        "judgment_id": "jdg-none", "bundle_digest": "sha256:" + "b" * 64, "deployment": record})
    assert (receipt["state"], receipt["error_code"]) == ("REJECTED", "JUDGMENT_MISSING")
    with pytest.raises(TypedRpcRemoteError) as exc:
        served.call("ai_research", "register_ai_deployment", {"deployment": record})
    assert exc.value.code == "VALIDATION_ERROR"
```

(`loop_thread`: import the fixture from `tests/sp1_acceptance/conftest.py` by adding `from tests.sp1_acceptance.conftest import loop_thread  # noqa: F401`.)

- [ ] **Step 3: Run** → fails (`PERMISSION_DENIED` / `METHOD_NOT_ALLOWED` for the new methods; the registration body is refused).
- [ ] **Step 4: Implement**

`trader/messaging/ai_deployment_wire.py`: strict (`extra="forbid", strict=True`) models `WithdrawAiDeploymentRequest(version_digest: Digest, reason: str 1–200)`, `GetAiDeploymentVersionRequest(version_digest)`, `GetActiveAiDeploymentsRequest()` (no fields), `ActiveAiDeployment` (fields as in Cross-plan additions; `strategy_path` and `class_name` with the `AiDeployment` patterns, `conids: list[int > 0]` of length 1–20, `expiry_session` `^\d{4}-\d{2}-\d{2}$`), `GetActiveAiDeploymentsResponse(account_mode: Literal["paper"], deployments: list[ActiveAiDeployment])`; `Digest = Annotated[str, Field(pattern=r"^sha256:[0-9a-f]{64}$")]`. The strategy service imports this module, never `production_api`.

`production_api.py`: `RegisterAiDeploymentRequest` gains `judgment_id: Annotated[str, Field(pattern=r"^[A-Za-z0-9_.:-]{1,128}$")]` and `bundle_digest: Annotated[str, Field(pattern=r"^sha256:[0-9a-f]{64}$")]`. Handlers:

```python
def _register_ai_deployment_rpc_handler(coordinator, account_id, actions):
    from trader.automation.ai_deployments import AiDeployment, deployment_digest
    from trader.automation.ai_paper_actions import REGISTER_ACTION

    def _handler(parsed: RegisterAiDeploymentRequest, caller: RpcCaller) -> Dict[str, Any]:
        deployment = AiDeployment.from_json(parsed.deployment)       # canonical: reordered conids replay
        body = {"judgment_id": parsed.judgment_id, "bundle_digest": parsed.bundle_digest,
                "deployment": deployment.to_json()}
        request = CommandRequest(
            command_id=actions.registration_command_id(body), action=REGISTER_ACTION, account_id=account_id,
            target_type="ai_deployment", target_id=deployment_digest(deployment), expected_version=None,
            body=body, source=caller.principal, principal=caller.principal)
        return _receipt_to_dict(coordinator.execute(request))
    return _handler
```

`_withdraw_ai_deployment_rpc_handler(coordinator, account_id)` builds a ledger `CommandRequest` with action `WITHDRAW_ACTION`, target `("ai_deployment_version", version_digest)`, body `{version_digest, reason}` and command id `"aidw-" + sha256(sorted-key JSON of the body)[:48]`. `_ai_deployment_read_handler(read, allowed)` refuses `PERMISSION_DENIED` (`_DispatchProblem`) when `caller.principal not in allowed`, then returns `read(parsed)`: each read checks the caller again (spec 5.1).

In `register_ai_paper_authority`: register `WITHDRAW_ACTION` on the coordinator (`requires_preflight=False`), pass `ai_paper.actions` to the register handler, and add:

```python
    registry.register("command", "withdraw_ai_deployment", WithdrawAiDeploymentRequest, dict,
                      _withdraw_ai_deployment_rpc_handler(coordinator, account_id), with_caller=True)
    registry.register("query", "get_ai_deployment_version", GetAiDeploymentVersionRequest, dict,
                      _ai_deployment_read_handler(lambda p: ai_paper.actions.version_view(p.version_digest),
                                              TRADER_ACL[("query", "get_ai_deployment_version")]), with_caller=True)
    registry.register("query", "get_active_ai_deployments", GetActiveAiDeploymentsRequest,
                      GetActiveAiDeploymentsResponse,
                      _ai_deployment_read_handler(lambda p: GetActiveAiDeploymentsResponse.model_validate(
                          ai_paper.actions.active_view()), frozenset({"strategy"})), with_caller=True)
```

`principals.py` (`TRADER_ACL`, after `get_ai_deployment`):

```python
    # SP2c Plan 2 (spec 5.1 table): explicit sets per method.
    ("query", "get_ai_deployment_version"): frozenset({"cli", "dashboard", "ai_supervisor", "ai_research"}),
    ("command", "withdraw_ai_deployment"): frozenset({"cli", "dashboard"}),
    ("query", "get_active_ai_deployments"): frozenset({"strategy"}),
```

`command_stack.py`:
- migrations: `apply_ai_deployment_version_migrations(migrator)  # 115, 116 (SP2c Plan 2)` after 56.
- `_AiPaperParts` gains `versions: Any = None`, `activity: Any = None`, `registrar: Any = None`, `judgments: Any = None`. Plan 1's `BacktestJudgments` construction moves from `_build_ai_paper_services` into `_build_ai_paper_parts` (the dispatch gate needs it before the saga exists); `_build_ai_paper_services` passes `judgments=parts.judgments` to `AiPaperServices`. In `_build_ai_paper_parts`:

```python
    from pathlib import Path
    from trader.automation.ai_bundle_check import DEFAULT_ARTIFACTS_ROOT, DEFAULT_VERIFY_DIR, ResearchBundleCheck
    from trader.automation.ai_deployment_activity import DeploymentActivity
    from trader.automation.ai_deployment_registration import AiDeploymentRegistrar
    from trader.automation.ai_deployment_versions import AiDeploymentVersionStore
    from trader.automation.ai_judgment_port import cooldown_reader_for, judgment_reader_for
    from trader.automation.backtest_judgments import BacktestJudgments

    deployments = AiDeploymentStore(trader.journal_db, now=now)
    artifacts_root = Path(getattr(trader, "research_artifacts_root", "") or DEFAULT_ARTIFACTS_ROOT).expanduser()
    verify_dir = Path(getattr(trader, "research_verify_dir", "") or DEFAULT_VERIFY_DIR).expanduser()
    cases_dir = artifacts_root / "cases"                          # Plan 1's default_cases_dir() layout
    judge = config.backtest_judge
    backtest_judgments = BacktestJudgments(trader.journal_db, config=judge, calendar=XNYSCalendarPolicy(),
                                           cases_dir=cases_dir, verify_dir=verify_dir, now=now)
    judgments = judgment_reader_for(backtest_judgments, cases_dir=cases_dir, verify_dir=verify_dir)
    cooldowns = cooldown_reader_for(trader.journal_db)
    versions = AiDeploymentVersionStore(trader.journal_db, now=now)
    activity = DeploymentActivity(versions=versions, deployments=deployments, judgments=judgments,
                                  cooldowns=cooldowns, max_active=judge.max_active_deploys, now=now)
    registrar = AiDeploymentRegistrar(
        db=trader.journal_db, deployments=deployments, versions=versions, activity=activity, judgments=judgments,
        cooldowns=cooldowns, bundles=ResearchBundleCheck(artifacts_root=artifacts_root, verify_dir=verify_dir),
        calendar=XNYSCalendarPolicy(), expiry_sessions=judge.deploy_expiry_sessions, now=now)
    trader.ai_deployment_versions, trader.ai_deployment_activity = versions, activity   # Plans 1 and 3 read these
```

and pass `versions`, `activity`, `registrar`, `judgments=backtest_judgments` into `_AiPaperParts`, and `versions`, `activity`, `registrar` into `AiPaperActions(...)` and `AiPaperServices(...)`.

`tests/test_rpc_acl.py`: add the two `cli`/`dashboard`/AI rows to `AI_PAPER_FAMILY`; the `ai_research` assertion becomes `{register_ai_deployment, get_ai_deployment, get_ai_deployment_version}`; add `assert TRADER_ACL[("query", "get_active_ai_deployments")] == {"strategy"}` (kept out of `AI_PAPER_FAMILY`, which forbids `strategy`). `tests/test_ai_paper_rpc.py`: `register(served)` uses `seed_judged_deployment`; `valid_body("register_ai_deployment")` adds `judgment_id` and `bundle_digest`; `test_supervisor_publishes_and_research_registers` becomes "refused `JUDGMENT_MISSING` without a judgment"; `test_reregistering_with_reordered_conids_replays` keeps its assertion with the new body (same command id because the body is canonical).

`tests/test_ai_policy_operator.py::test_ai_research_rights_are_exactly_the_sp1_set`: the exact set (Plan 1 already added the two judgment methods) also gains `("query", "get_ai_deployment_version")`.

- [ ] **Step 5: Run** `tests/automation/test_ai_deployment_rpc.py tests/test_rpc_acl.py tests/test_ai_paper_rpc.py tests/test_ai_policy_operator.py` → pass.
- [ ] **Step 6: Commit** `feat: serve ai deployment versions, withdrawal and the active set over signed rpc`.

### Task 7: Decision binding and the admission recheck

**Files:**
- Modify: `trader/automation/ai_paper_decision.py`, `trader/messaging/production_api.py` (`SubmitAiPaperDecisionRequest`), `tests/automation/test_ai_paper_decision_model.py`
- Test: `tests/automation/test_ai_paper_version_recheck.py` (admission part)

**Interfaces:**
- Consumes: `DeploymentActivity.entry_refusal`.
- Produces: `AiPaperDecision.deployment_version`, `.source_digest`; `_KEYS` gains both; `ai_paper_decisions` columns `deployment_version`, `source_digest`; `AiPaperDecisionService(..., activity=None)`.

- [ ] **Step 1: Write the failing model tests** (append to `test_ai_paper_decision_model.py`; first add `"deployment_version": "sha256:" + "d" * 64, "source_digest": "sha256:" + "a" * 64` to `ENTER` and `None` for both in `CLOSE`):

```python
@pytest.mark.parametrize("change", [{"deployment_version": "abc"}, {"source_digest": "sha256:ABC"},
                                    {"deployment_version": None}])
def test_enter_binding_fields_are_strict_and_paired(change):
    with pytest.raises(DecisionInvalid):
        AiPaperDecision.from_body({**ENTER, **change})


def test_a_reduction_carries_no_binding():
    with pytest.raises(DecisionInvalid):
        AiPaperDecision.from_body({**CLOSE, "deployment_version": "sha256:" + "d" * 64,
                                   "source_digest": "sha256:" + "a" * 64})
```

- [ ] **Step 2: Run** `tests/automation/test_ai_paper_decision_model.py` → fails.
- [ ] **Step 3: Implement** in `ai_paper_decision.py`:
  - dataclass fields at the end: `deployment_version: Optional[str] = None`, `source_digest: Optional[str] = None`; `_KEYS` appends both.
  - `_check_fields`: each is null or `sha256:<64 hex>`; `(deployment_version is None) != (source_digest is None)` → `DecisionInvalid("deployment_version and source_digest come together")`.
  - `_check_shape` for reductions: `if self.deployment_version is not None or self.source_digest is not None: raise DecisionInvalid(f"{self.action} carries no deployment binding")`.
  - migration 56 CREATE: append `deployment_version VARCHAR, source_digest VARCHAR` after `deployment_kind VARCHAR`; `_ROW_COLUMNS` and `DecisionRow` gain both (default None); `with_decision` copies both.
  - service: constructor `activity: Any = None`; `_deployment` becomes:

```python
    def _deployment(self, decision: AiPaperDecision, admission: _Admission) -> Any:
        try:
            deployment = self._deployments.get_sealed_any(decision.deployment_digest)
        except DeploymentRefused as ex:
            raise _Refusal(ex.code, detail=ex.message) from None
        if isinstance(deployment, DiscretionaryDeployment):
            admission.row = replace(admission.row, style=deployment.style, deployment_kind=DISCRETIONARY_KIND)
            if decision.deployment_version is not None:
                raise _Refusal("DEPLOYMENT_VERSION_UNEXPECTED")
        else:
            admission.row = replace(admission.row, strategy_digest=deployment.strategy_digest,
                                    style=deployment.style, deployment_kind=STRATEGY_KIND)
            if deployment.decider_verdict != "DEPLOY":
                raise _Refusal("DEPLOYMENT_NOT_DEPLOYABLE")
            if decision.conid not in deployment.conids:
                raise _Refusal("CONID_NOT_IN_DEPLOYMENT")
            self._require_active_version(decision)
        if not self._config.style_enabled(deployment.style):
            raise _Refusal(STYLE_NOT_ENABLED)
        return deployment

    def _require_active_version(self, decision: AiPaperDecision) -> None:
        """SP2c spec 5.2 item 8 at admission; the dispatch gate checks again right before the send."""
        if self._activity is None:
            raise _Refusal("DEPLOYMENT_STATE_UNAVAILABLE", retryable=True)     # fail closed when unwired
        try:
            code = self._activity.entry_refusal(deployment_digest=decision.deployment_digest,
                                                version_digest=decision.deployment_version,
                                                source_digest=decision.source_digest)
        except Exception:
            logger.exception("deployment version state unavailable for %s", decision.decision_id)
            raise _Refusal("DEPLOYMENT_STATE_UNAVAILABLE", retryable=True) from None
        if code:
            raise _Refusal(code)
```

  - `SubmitAiPaperDecisionRequest`: `deployment_version: Optional[str] = None`, `source_digest: Optional[str] = None` (shape is checked by the model above).
- [ ] **Step 4: Run** `tests/automation/test_ai_paper_decision_model.py` → pass. World-based ENTER tests go red here (no `activity` yet) and pass again in Task 8; run only the listed file.
- [ ] **Step 5: Commit** `feat: bind ai paper decisions to a deployment version and source digest`.

### Task 8: Dispatch gate, World and the recheck tests

**Files:**
- Modify: `trader/trading/command_stack.py` (`_ai_paper_guard_options`, `_build_ai_paper_services`), `tests/automation/ai_paper_world.py`
- Test: `tests/automation/test_ai_paper_version_recheck.py`

**Interfaces:**
- Consumes: `deployment_version_gate`, Task 7 service, `World.on_before_guard`.
- Produces: `World.version_digest`, `World.versions`, `World.activity`, `World.activity_clock`, `World.judgments`, `World.cooldowns`.

- [ ] **Step 1: Update World** (`ai_paper_world.py`): apply `apply_ai_deployment_version_migrations`; after registering `GOOD`, seal a version and build the activity on its own clock:

```python
        self.activity_clock = Clock()
        self.judgments, self.cooldowns = SeededJudgments(), Cooldowns()
        self.versions = AiDeploymentVersionStore(self.db, now=self.clock)
        self.activity = DeploymentActivity(versions=self.versions, deployments=self.deployments,
                                           judgments=self.judgments, cooldowns=self.cooldowns, max_active=3,
                                           now=self.activity_clock)
        self.version_digest = self.seal_version("jdg-world-1")
```

```python
    def seal_version(self, judgment_id, *, first=dt.date(2026, 7, 17), expiry=dt.date(2026, 8, 14)):
        self.judgments.seed(deploy_facts(GOOD, judgment_id))
        version = DeploymentVersion(self.digest, judgment_id, INITIAL, None, first, expiry)
        return self.db.transaction(lambda conn: self.versions.seal_in_tx(
            conn, version, request_digest="sha256:" + hashlib.sha256(judgment_id.encode()).hexdigest(),
            principal="ai_research", command_id=f"seed-{judgment_id}"))[0]
```

`enter_body` gains `"deployment_version": None, "source_digest": None`; `World.body`:

```python
    def body(self, **changes):
        if changes.get("deployment_digest", self.digest) == self.digest and "deployment_version" not in changes:
            changes = {"deployment_version": self.version_digest, "source_digest": GOOD["strategy_digest"],
                       **changes}
        return enter_body(self.digest, self.clock(), **changes)
```

Pass `activity=self.activity` to `AiPaperDecisionService` and compose the gate: `compose_entry_gates(self._scope_gate, deployment_version_gate(kind_of=self.deployments.kind_of, activity=self.activity), ai_entry_gate(entry_filter=self.entry_filter))`. Reduction bodies (`close_body` sets `deployment_digest=None`) and discretionary bodies (another digest) get no binding from `World.body`, so their tests need no change.

- [ ] **Step 2: Write the failing tests**

```python
"""SP2c Plan 2 Task 8: recheck at admission and at final dispatch; exits never blocked (spec 5.2 item 8, 9)."""
from __future__ import annotations

import datetime as dt

import pytest

from tests.automation.ai_paper_fixtures import CONID
from tests.automation.ai_paper_world import GOOD, World
from tests.automation.test_ai_paper_reductions import close_body

KEY = "strategies/opening_range_breakout.py:OpeningRangeBreakout"


@pytest.fixture
def world(tmp_path):
    return World(tmp_path, real_liquidation=True)


def test_an_active_version_enters(world):
    assert world.submit().state == "SUBMITTED"
    assert world.decisions.row("dec-00000001").deployment_version == world.version_digest


@pytest.mark.parametrize("change,code", [
    ({"deployment_version": None, "source_digest": None}, "DEPLOYMENT_VERSION_REQUIRED"),
    ({"source_digest": "sha256:" + "f" * 64}, "STRATEGY_SOURCE_MISMATCH"),
    ({"deployment_version": "sha256:" + "e" * 64}, "DEPLOYMENT_NOT_ACTIVE")])
def test_admission_refuses_a_bad_binding(world, change, code):
    assert world.submit(**change).error_code == code


def test_expired_version_is_refused_at_admission(world):
    world.version_digest = world.seal_version("jdg-old", first=dt.date(2026, 6, 1), expiry=dt.date(2026, 7, 16))
    receipt = world.submit()
    assert (receipt.state, receipt.error_code) == ("REJECTED", "DEPLOYMENT_EXPIRED")
    assert world.dispatch.plans == []


def test_cooldown_refuses_at_admission(world):
    world.cooldowns.keys.add(KEY)
    assert world.submit().error_code == "FAMILY_COOLING_DOWN"


def test_withdrawal_between_jev_and_send_is_refused_at_dispatch(world):
    world.on_before_guard(lambda: world.versions.withdraw(world.version_digest, reason="op", principal="cli",
                                                          command_id="w"))
    receipt = world.submit()
    assert receipt.error_code == "DEPLOYMENT_NOT_ACTIVE" and world.dispatch.plans == []


def test_expiry_between_jev_and_send_is_refused_at_dispatch(world):
    world.version_digest = world.seal_version("jdg-last", expiry=dt.date(2026, 7, 17))
    world.on_before_guard(lambda: world.activity_clock.advance(days=1))
    receipt = world.submit()
    assert receipt.error_code == "DEPLOYMENT_EXPIRED" and world.dispatch.plans == []


def test_an_exit_on_the_same_conid_still_goes_out(world):
    world.owned(CONID, 300.0)
    world.versions.withdraw(world.version_digest, reason="op", principal="cli", command_id="w")
    world.cooldowns.keys.add(KEY)
    world.activity_clock.advance(days=60)
    assert world.submit(close_body(world, decision_id="dec-close-01")).error_code == "CLOSE_PENDING"
```

- [ ] **Step 3: Run** → the dispatch tests fail until the gate is composed in `command_stack.py` too.
- [ ] **Step 4: Implement** `_ai_paper_guard_options`: `compose_entry_gates(discretionary_scope_gate(...), deployment_version_gate(kind_of=parts.deployments.kind_of, activity=parts.activity), ai_entry_gate(entry_filter=parts.entry_filter))`; `_build_ai_paper_services`: `AiPaperDecisionService(..., activity=parts.activity)`.
- [ ] **Step 5: Run** `tests/automation/test_ai_paper_version_recheck.py tests/automation/test_ai_paper_entry.py tests/automation/test_ai_paper_reductions.py tests/automation/test_ai_paper_epoch.py tests/automation/test_ai_paper_experiment_integration.py tests/automation/test_discretionary*.py` → pass.
- [ ] **Step 6: Commit** `feat: recheck the ai deployment version at admission and final dispatch`.

### Task 9: Signal binding and the strategy service second source

**Files:**
- Modify: `trader/data/strategy_signal_record.py`, `trader/strategy/strategy_runtime.py`
- Create: `trader/strategy/ai_deployment_source.py`, `tests/strategy/__init__.py`, `tests/strategy/ai_deployment_fixtures.py`
- Test: `tests/test_strategy_ai_deployments.py`, `tests/test_strategy_signal_record.py` (binding round trip)

**Interfaces:**
- Consumes: `GetActiveAiDeploymentsResponse`, `ActiveAiDeployment`, `compute_strategy_hash`.
- Produces: `SignalEntry.deployment_digest / deployment_version / source_digest` (all three or none); `AI_INSTANCE_PREFIX = "aidv-"`; `ai_instance_name(version_digest)`; `AiInstanceBinding(version_digest, base_digest, strategy_digest)`; `source_unchanged(instance) -> bool`; `AiDeploymentSource(*, runtime, read_active, paper)` with `reconcile()`; `StrategyRuntime.unload_strategy(name) -> bool`, `load_ai_deployment(deployment) -> bool`, `ai_instances() -> dict[str, Strategy]`; `StrategyNode`.

- [ ] **Step 1: Write the failing tests** (`tests/test_strategy_ai_deployments.py`; `_make_runtime` and `_write_strategy` as in `tests/test_strategy_artifact_soft_load.py`, plus `rt._load_enabled = lambda name: None`, `rt.signal_record = StrategySignalRecord(DuckDBConnection.get_instance(tmp_duckdb_path))`, `rt.event_store = SimpleNamespace(append=lambda e: None)`, `rt.zmq_messagebus_client = SimpleNamespace(write=lambda *a: None)`):

```python
def active(path, *, digest=None, version="sha256:" + "d" * 64):
    return ActiveAiDeployment(
        version_digest=version, base_digest="sha256:" + "b" * 64,
        strategy_path="strategies/" + os.path.basename(path),
        strategy_digest=digest or "sha256:" + compute_strategy_hash(path), class_name="VwapReclaimCat",
        params={}, conids=[265598], bar_size="1 min", expiry_session="2026-11-06")


def source(rt, deployments, *, paper=True):
    return AiDeploymentSource(runtime=rt, read_active=lambda: deployments, paper=paper)


def test_an_active_deployment_loads_under_its_version_name(rt, path):
    source(rt, [active(path)]).reconcile()
    instance = rt.get_strategy(ai_instance_name("sha256:" + "d" * 64))
    assert instance is not None and instance.ai_deployment_version == "sha256:" + "d" * 64


def test_a_changed_file_is_refused_at_load(rt, path):
    source(rt, [active(path, digest="sha256:" + "0" * 64)]).reconcile()
    assert rt.ai_instances() == {}


def test_a_file_replaced_after_load_drops_signals_and_unloads(rt, path):
    deployment = active(path)
    src = source(rt, [deployment])
    src.reconcile()
    instance = rt.get_strategy(ai_instance_name(deployment.version_digest))
    with open(path, "a") as f:
        f.write("# replaced\n")
    rt._dispatch_signal(instance, Signal(source_name=instance.name, action=Action.BUY, probability=0.5),
                        conId=265598, frame=_frame())
    assert rt.signal_record.read(0, 10).signals == ()
    src.reconcile()
    assert rt.ai_instances() == {}


def test_withdrawn_or_expired_deployments_unload(rt, path):
    src = source(rt, [active(path)])
    src.reconcile()
    src._read_active = lambda: []
    src.reconcile()
    assert rt.ai_instances() == {}


def test_nothing_loads_on_live(rt, path):
    source(rt, [active(path)], paper=False).reconcile()
    assert rt.ai_instances() == {}


def test_signals_carry_the_binding(rt, path):
    deployment = active(path)
    source(rt, [deployment]).reconcile()
    instance = rt.get_strategy(ai_instance_name(deployment.version_digest))
    rt._dispatch_signal(instance, Signal(source_name=instance.name, action=Action.BUY, probability=0.5),
                        conId=265598, frame=_frame())
    (recorded,) = rt.signal_record.read(0, 10).signals
    assert (recorded.entry.deployment_version, recorded.entry.source_digest, recorded.entry.deployment_digest) == (
        deployment.version_digest, deployment.strategy_digest, deployment.base_digest)


def test_a_config_strategy_cannot_take_an_ai_name(rt, path):
    rt.load_strategy(name="aidv-0123456789abcdef", bar_size_str="1 min", conids=[265598], universe=None,
                     historical_days_prior=1, module=path, class_name="VwapReclaimCat", description="x")
    assert rt.get_strategy("aidv-0123456789abcdef") is None
```

`_frame()` is a one-row OHLCV frame with a tz-aware `DatetimeIndex` named `date`.

- [ ] **Step 2: Run** → fails (`ModuleNotFoundError: trader.strategy.ai_deployment_source`).
- [ ] **Step 3: Implement**

`strategy_signal_record.py`: CREATE gains `deployment_digest VARCHAR, deployment_version VARCHAR, source_digest VARCHAR`; `_COLUMNS` appends them; `SignalEntry` appends three `Optional[str] = None` fields; `create(..., deployment_digest=None, deployment_version=None, source_digest=None)` checks all-or-none and the `sha256:<64 hex>` shape (`ValueError` otherwise); `append_in_tx` writes them; `_read_in_tx` reads them; `RecordedSignal.to_json` adds the three keys.

`trader/strategy/ai_deployment_source.py`:

```python
"""The strategy service's second source: active AI deployments from the trader (SP2c spec 5.4).

Paper only. An instance runs the exact bytes whose hash the trader marked active,
under a name derived from the version digest; anything else is unloaded.
"""
from __future__ import annotations

import hmac
import logging
from dataclasses import dataclass
from typing import Any, Callable, Sequence

from trader.data.backtest_store import compute_strategy_hash
from trader.messaging.typed_rpc import TypedRpcRemoteError

AI_INSTANCE_PREFIX = "aidv-"
AI_HISTORY_DAYS = 5


@dataclass(frozen=True)
class AiInstanceBinding:
    version_digest: str
    base_digest: str
    strategy_digest: str


def ai_instance_name(version_digest: str) -> str:
    return AI_INSTANCE_PREFIX + version_digest.split(":", 1)[1][:16]


def source_unchanged(instance: Any) -> bool:
    current = compute_strategy_hash(getattr(instance, "ai_source_path", ""))
    return bool(current) and hmac.compare_digest("sha256:" + current, instance.ai_source_digest)


class AiDeploymentSource:
    def __init__(self, *, runtime: Any, read_active: Callable[[], Sequence[Any]], paper: bool):
        self._runtime, self._read_active, self._paper = runtime, read_active, paper

    def reconcile(self) -> None:
        loaded = self._runtime.ai_instances()
        if not self._paper:
            for name in loaded:
                self._runtime.unload_strategy(name)
            return
        try:
            active = list(self._read_active())
        except (TimeoutError, ConnectionError) as ex:
            logging.warning("active AI deployments unavailable; keeping the loaded set: %s", ex)
            return
        except TypedRpcRemoteError as ex:
            if ex.code != "METHOD_NOT_ALLOWED":
                raise
            active = []                                   # ai_paper is off: nothing may run
        wanted = {ai_instance_name(d.version_digest): d for d in active}
        for name, instance in loaded.items():
            if name not in wanted:
                logging.info("unloading AI deployment %s: no longer active", name)
                self._runtime.unload_strategy(name)
            elif not source_unchanged(instance):
                logging.error("unloading AI deployment %s: its file changed after load", name)
                self._runtime.unload_strategy(name)
        for name, deployment in wanted.items():
            if self._runtime.get_strategy(name) is None and not self._runtime.load_ai_deployment(deployment):
                logging.error("AI deployment %s did not load", deployment.version_digest)
```

`strategy_runtime.py`:
- Extract `unload_strategy(self, name) -> bool` from the two duplicated blocks in `update_strategy_params` and `_swap_runtime` (remove from `strategy_implementations` and every `self.strategies` list, drop `_last_dispatched_bar` keys, pop `sys.modules[f'_mmr_strategy_{name}']`); both callers use it.
- `ai_instances(self)`: `{s.name: s for s in self.strategy_implementations if getattr(s, 'ai_deployment_version', None)}`.
- `load_strategy(..., ai_binding: Optional[AiInstanceBinding] = None)`. First line after the duplicate-name check: `if name.startswith(AI_INSTANCE_PREFIX) and ai_binding is None: logging.error('refusing to load strategy %s: the %s prefix is reserved for AI deployments', name, AI_INSTANCE_PREFIX); return`. After `loaded_source_digest` is computed:

```python
        if ai_binding is not None and not hmac.compare_digest('sha256:' + loaded_source_digest,
                                                              ai_binding.strategy_digest):
            logging.error('refusing to load AI deployment %s: %s is not the judged file', name, module)
            return
```

  After `instance.loaded_source_digest = loaded_source_digest`:

```python
                if ai_binding is not None:
                    instance.ai_deployment_version = ai_binding.version_digest
                    instance.ai_deployment_digest = ai_binding.base_digest
                    instance.ai_source_digest = ai_binding.strategy_digest
                    instance.ai_source_path = filepath
```

  and for an AI instance with `persisted is None`, call `instance.enable()`.
- `load_ai_deployment(self, deployment) -> bool`:

```python
    def load_ai_deployment(self, deployment) -> bool:
        name = ai_instance_name(deployment.version_digest)
        self.load_strategy(
            name=name, bar_size_str=deployment.bar_size, conids=list(deployment.conids), universe=None,
            historical_days_prior=AI_HISTORY_DAYS, module=deployment.strategy_path.removeprefix('strategies/'),
            class_name=deployment.class_name, description=f'AI deployment {deployment.version_digest}',
            paper_only=True, auto_execute=False, params=dict(deployment.params),
            ai_binding=AiInstanceBinding(deployment.version_digest, deployment.base_digest,
                                         deployment.strategy_digest))
        return self.get_strategy(name) is not None
```

- `_dispatch_signal`, first statement:

```python
        if getattr(strategy, 'ai_deployment_version', None) and not source_unchanged(strategy):
            logging.error('AI deployment %s: %s changed after load; signal dropped, unloading at the next '
                          'reconcile', strategy.name, strategy.ai_source_path)
            strategy.disable()
            return
```

- `_record_signal` passes `deployment_digest=getattr(strategy, 'ai_deployment_digest', None)`, `deployment_version=getattr(strategy, 'ai_deployment_version', None)`, `source_digest=getattr(strategy, 'ai_source_digest', None)` to `SignalEntry.create`.
- `connect()`: after `_trader_query_client` exists, `self._ai_deployment_source = AiDeploymentSource(runtime=self, read_active=self._read_active_ai_deployments, paper=bool(self.paper_trading))` with `_read_active_ai_deployments` returning `self._trader_query_client.call('get_active_ai_deployments', {}, GetActiveAiDeploymentsResponse).deployments`.
- `_reconcile_sync`, after the config reload (step 1) and before re-subscribing (step 2):

```python
        source = getattr(self, '_ai_deployment_source', None)
        if source is not None:
            try:
                source.reconcile()
            except Exception as ex:
                logging.warning('AI deployment reconcile failed (will retry next cycle): %s', ex)
```

`tests/strategy/ai_deployment_fixtures.py` (Plan 4 acceptance): `StrategyNode(served, *, strategies_dir)` builds the runtime the same way as the test above, with `signal_record` on `served.trader.duckdb_path`, `read_active = lambda: served.client("strategy", "query").call("get_active_ai_deployments", {}, GetActiveAiDeploymentsResponse).deployments`; `.reconcile()` calls `AiDeploymentSource.reconcile()`; `.instances()` returns `{s.ai_deployment_version: s.name for s in rt.ai_instances().values()}`; `.feed_bar(conid, frame)` calls `instance.on_prices(frame)` for each instance that holds the conid and passes a non-None signal to `rt._dispatch_once(instance, signal, conid, frame)`.

- [ ] **Step 4: Run** `tests/test_strategy_ai_deployments.py tests/test_strategy_signal_record.py tests/test_strategy_artifact_soft_load.py tests/test_strategy_runtime*.py` → pass.
- [ ] **Step 5: Commit** `feat: load active ai deployments by executing-bytes hash in the strategy service`.

### Task 10: `ai` controller pass-through

**Files:**
- Modify: `trader/ai/engine.py` (`SignalOpportunity`, `ProposedDecision`, `enter_decision`), `trader/ai/signal_intake.py`, `trader/ai/runtime_schema.py` (migration 14 CREATE in place), `trader/ai/decision_engine.py` (pass-through; exit ownership by version, ruling 21), `trader/ai/submitter.py`, `trader/ai/config.py`
- Test: `tests/ai/test_signal_binding_pass_through.py`, `tests/ai/decisions/test_decision_engine.py` (append)

**Interfaces:**
- Consumes: `read_ai_signals` keys from Task 9.
- Produces: `SignalOpportunity.deployment_digest / deployment_version / source_digest` (defaults None); `ProposedDecision.deployment_version / source_digest`; `DecisionsConfig.ai_deployments: Bracketing`; `build_body` sends both keys; `PaperDecisionEngine._entry_versions(decision_ids) -> dict`, `_fillable_entries(conid, now, version)`; engine note `EXIT_CONID_SHARED`.

- [ ] **Step 1: Write the failing tests**

```python
"""SP2c Plan 2 Task 10: the controller copies the signal's deployment binding into the ENTER (ruling 19)."""
import datetime as dt

import pytest

from trader.ai.engine import ProposedDecision
from trader.ai.signal_intake import SignalIntakeError, _parse_signal
from trader.ai.submitter import build_body

D = {"deployment_digest": "sha256:" + "b" * 64, "deployment_version": "sha256:" + "d" * 64,
     "source_digest": "sha256:" + "a" * 64}
RAW = {"cursor": 1, "source_event_id": "sig-" + "0" * 32, "strategy_name": "aidv-dddddddddddddddd",
       "conid": 265598, "action": "BUY", "probability": 0.5, "signal_time": "2026-10-12T15:00:00+00:00",
       "recorded_at": "2026-10-12T15:00:01+00:00", **D}


def test_the_binding_reaches_the_opportunity():
    opportunity = _parse_signal(RAW)
    assert (opportunity.deployment_version, opportunity.source_digest) == (D["deployment_version"],
                                                                            D["source_digest"])


@pytest.mark.parametrize("change", [{"deployment_version": None}, {"source_digest": "x"}])
def test_a_partial_binding_is_malformed(change):
    with pytest.raises(SignalIntakeError):
        _parse_signal({**RAW, **change})


def test_the_enter_body_carries_the_binding():
    decision = ProposedDecision(action_key="enter:265598", action="ENTER", conid=265598, side="BUY",
                                decider="jev", evidence_digest="sha256:" + "e" * 64,
                                deployment_digest=D["deployment_digest"], policy_revision=1, stop_price=98.0,
                                deployment_version=D["deployment_version"], source_digest=D["source_digest"])
    body = build_body(decision, decision_id="dec-00000001",
                      expires_at=dt.datetime(2026, 10, 12, 15, 5, tzinfo=dt.timezone.utc))
    assert (body["deployment_version"], body["source_digest"]) == (D["deployment_version"], D["source_digest"])
```

Append to `tests/ai/decisions/test_decision_engine.py` (ruling 21; it reuses that file's `started`, `FakeReads`, `Submitter`, `broker`, `EXPERIMENT`, `AAPL`, `NOW`):

```python
# -- SP2c Plan 2 ruling 21 (PR #91 thread 4218219168): a SELL closes only its own version's trips ------------------

AI_DEPLOYMENT, SOURCE = "sha256:" + "c" * 64, "sha256:" + "5" * 64
V_A, V_B = "sha256:" + "a" * 64, "sha256:" + "b" * 64


def bound(opportunity_id, cursor, action, version):
    return SignalOpportunity(opportunity_id, cursor, "aidv-" + version[7:23], AAPL, action, 0.7, NOW, NOW,
                             deployment_digest=AI_DEPLOYMENT, deployment_version=version, source_digest=SOURCE)


def bound_enter(rig, opportunity):
    """The ENTER the controller sent for a version-bound BUY; its stored body carries the version."""
    enter = ProposedDecision(action_key=f"enter:{opportunity.conid}", action="ENTER", conid=opportunity.conid,
                             side="BUY", decider="jev", evidence_digest="sha256:" + "f" * 64,
                             deployment_digest=AI_DEPLOYMENT, policy_revision=1, stop_price=225.4,
                             target_price=239.2, deployment_version=opportunity.deployment_version,
                             source_digest=SOURCE)
    submitter = Submitter(store=rig.store, supervisor=None, leadership=None, clock=rig.clock, slots=None,
                          experiment_state=lambda: None)
    decision_id = rig.store.transaction(lambda conn: submitter.insert_in_tx(
        conn, source_kind="entry_signal", source_id=opportunity.opportunity_id, decision=enter,
        expires_at=NOW + dt.timedelta(minutes=5), epoch=1, now=NOW))
    rig.store.db.execute("UPDATE ai_submissions SET state = 'FINAL', receipt_state = 'RESOLVED' "
                         "WHERE decision_id = ?", [decision_id])
    return decision_id


def trip(trip_id, decision_id, state="OPEN"):
    return {"round_trip_id": trip_id, "conid": AAPL, "symbol": "AAPL", "opened_at": NOW.isoformat(),
            "opened_quantity": 5.0, "closed_quantity": 5.0 if state == "CLOSED" else 0.0,
            "decision_id": decision_id, "state": state, "entry_avg_price": 230.0}


SELL_A = bound("sig-" + "a" * 32, 20, "SELL", V_A)                      # queued while A was still active


@pytest.mark.asyncio
async def test_an_old_version_sell_never_closes_a_successor_trip(tmp_path):
    trips = {"experiment_id": EXPERIMENT.experiment_id, "trips": []}
    rig = await started(tmp_path, FakeReads(get_experiment_trips=lambda body: trips,
                                            get_broker_order_evidence=lambda body: broker()))
    enter_b = bound_enter(rig, bound("sig-" + "c" * 32, 22, "BUY", V_B))       # B superseded A and bought AAPL
    trips["trips"] = [trip("rt-b", enter_b)]
    result = await rig.engine.on_exit_signal(rig.signal(SELL_A))
    assert (result.decisions, result.note) == ((), "NOT_HELD")                   # B's trip and entry are not A's
    enter_a = bound_enter(rig, bound("sig-" + "b" * 32, 21, "BUY", V_A))
    trips["trips"] = [trip("rt-a", enter_a), trip("rt-b", enter_b)]
    result = await rig.engine.on_exit_signal(rig.signal(SELL_A))
    assert (result.decisions, result.note) == ((), "EXIT_CONID_SHARED")          # a conid-wide CLOSE would hit B


@pytest.mark.asyncio
async def test_an_old_version_sell_still_closes_its_own_trip_after_supersession(tmp_path):
    trips = {"experiment_id": EXPERIMENT.experiment_id, "trips": []}
    rig = await started(tmp_path, FakeReads(get_experiment_trips=lambda body: trips))
    enter_a = bound_enter(rig, bound("sig-" + "b" * 32, 21, "BUY", V_A))
    enter_b = bound_enter(rig, bound("sig-" + "c" * 32, 22, "BUY", V_B))       # B is the active version now
    trips["trips"] = [trip("rt-a", enter_a), trip("rt-b", enter_b, state="CLOSED")]
    (close,) = (await rig.engine.on_exit_signal(rig.signal(SELL_A))).decisions
    assert (close.action, close.conid, close.decider, close.side) == ("CLOSE", AAPL, "strategy", "SELL")
    assert (close.deployment_version, close.source_digest) == (None, None)       # a reduction carries no binding
    assert rig.calls("get_ai_deployment_version") == []                          # no active-version check on exits
```

- [ ] **Step 2: Run** → fails.
- [ ] **Step 3: Implement.**
  - `SignalOpportunity` and `ProposedDecision`: three / two `Optional[str] = None` fields at the end; `ProposedDecision.__post_init__` checks each is null or a digest and that version and source come together.
  - `_parse_signal`: read the three keys with `.get`; all None or all `sha256:<64 hex>` (else `SIGNAL_MALFORMED`); pass to `SignalOpportunity`.
  - `runtime_schema.py` migration 14: `ai_opportunities` gains `deployment_digest VARCHAR, deployment_version VARCHAR, source_digest VARCHAR` before `state`; `signal_intake._COLUMNS`, the `INSERT` and `_opportunity` follow.
  - `config.py`: `DecisionsConfig.ai_deployments: Bracketing = Field(default_factory=Bracketing)`.
  - `decision_engine.on_entry_signal`:

```python
        opportunity = ctx.opportunity
        if opportunity.deployment_version is not None:
            # An AI-deployment instance: its own base digest; the trader checks version, cap and source.
            strategy = StrategyBracket(deployment_digest=opportunity.deployment_digest,
                                       stop_fraction=self._cfg.ai_deployments.stop_fraction,
                                       target_fraction=self._cfg.ai_deployments.target_fraction)
        else:
            strategy = self._cfg.strategies.get(opportunity.strategy_name)
```

    and `enter_decision(action_key, judgment, binding=opportunity)` sets `deployment_version=binding.deployment_version, source_digest=binding.source_digest` (`binding` defaults to None for self-found entries).
  - `submitter.build_body`: adds `"deployment_version": decision.deployment_version, "source_digest": decision.source_digest`.
  - `decision_engine.on_exit_signal` (ruling 21): after the trips are read, replace the conid-only `held` check with:

```python
        here = [p for p in owned_positions_from_trips(trips) if p.conid == opportunity.conid]
        versions = await self._entry_versions(tuple(p.decision_id for p in here if p.decision_id))
        mine = [p for p in here if versions.get(p.decision_id) == opportunity.deployment_version]
        if not mine:
            return await self._exit_before_any_fill(ctx, trips)
        if len(mine) < len(here):
            logger.warning("exit %s: conid %s is also held by another deployment version; no CLOSE",
                           opportunity.opportunity_id, opportunity.conid)
            return EngineResult(note="EXIT_CONID_SHARED")
```

    The CLOSE that follows is unchanged (no binding, ruling 9). Add:

```python
    async def _entry_versions(self, decision_ids: tuple[str, ...]) -> dict[str, Optional[str]]:
        """Ruling 21: the version each trip's ENTER was bound to, from the bodies this controller sent."""
        if not decision_ids:
            return {}
        marks = ", ".join("?" for _ in decision_ids)
        rows = await self._store.aquery(f"SELECT decision_id, body_json FROM ai_submissions "
                                        f"WHERE action = 'ENTER' AND decision_id IN ({marks})", list(decision_ids))
        return {decision_id: json.loads(body_json).get("deployment_version") for decision_id, body_json in rows}
```

    `_exit_before_any_fill` calls `self._fillable_entries(conid, ctx.now, ctx.opportunity.deployment_version)`; `_fillable_entries(conid, now, version)` also skips an ENTER whose `json.loads(body_json).get("deployment_version") != version`, so an A SELL never waits on a B entry. A trip whose ENTER is unknown here, or an unbound ENTER, has version `None`: an unbound SELL keeps today's behaviour for unbound trips and never closes a bound one.
- [ ] **Step 4: Run** `tests/ai/test_signal_binding_pass_through.py tests/ai/` (directory, `-x`) → pass.
- [ ] **Step 5: Commit** `feat: carry the signal's deployment binding into the ai enter`.

### Task 11: Harness migration, SP1 acceptance and the full suite

**Files:**
- Modify: `tests/ai/runtime/trader_world.py`, `tests/test_ai_controller_rpc.py`, `tests/ai/decisions/*` helpers that append `SignalEntry`, `trader/acceptance/scenario.py`, `trader/acceptance/runner.py`, `trader/mmr_cli.py` (`acceptance run --deployment-version`), `tests/sp1_acceptance/*`
- Test: existing suites

**Interfaces:**
- Consumes: `install_seeded_judgments`, `seed_judged_deployment` (Task 6).
- Produces: `AcceptanceSettings.deployment_version: str`.

- [ ] **Step 1: Migrate the served harnesses.** `TraderWorld.__init__`: `self.seeded = install_seeded_judgments(monkeypatch)` before `served_stack(...)`; replace the `register_ai_deployment` call by `self.digest, self.version = seed_judged_deployment(self.served.composed.stack.ai_paper, self.seeded, deployment_record(chosen), today=self.served.now().date())`; `self.source_digest = deployment_record(chosen)["strategy_digest"]`; `enter()` passes `deployment_version=self.version, source_digest=self.source_digest`. Every test helper that appends a `SignalEntry` for the configured strategy passes `deployment_digest=world.digest, deployment_version=world.version, source_digest=world.source_digest` (find them with `grep -rn "SignalEntry.create" tests`).
- [ ] **Step 2: SP1 acceptance reads a judged version (ruling 18).** In `scenario.py`: `AcceptanceSettings.deployment_version: str = ""`; `RUN_STEPS` replaces `"register"` with `"deployment"`; `planned_calls` replaces `("research", "register_ai_deployment")` with `("supervisor", "get_ai_deployment_version"), ("supervisor", "get_ai_deployment")`; `_absorb` also carries `deployment_version` and `source_digest`; `build_entry` adds `"deployment_version": ctx["deployment_version"], "source_digest": ctx["source_digest"]`; `build_entry_s`'s default ctx and `build_reduction` add both as `None`. Replace `_step_register` with:

```python
    def _step_deployment(self) -> StepResult:
        """Ruling 18: the run trades under an operator-given SP2c deployment version; it registers nothing."""
        digest = self.settings.deployment_version
        reply = self.port.supervisor("get_ai_deployment_version", {"version_digest": digest})
        version = reply.get("version") if reply.get("found") else None
        if version is None or version["state"] != "ACTIVE":
            raise StepFailure("DEPLOYMENT_NOT_USABLE", {"version": version})
        record = self.port.supervisor("get_ai_deployment", {"digest": version["base_digest"]}).get("deployment") or {}
        if not {self.settings.conid_a, self.settings.conid_b} <= set(record.get("conids") or ()):
            raise StepFailure("DEPLOYMENT_NOT_USABLE", {"conids": record.get("conids")})
        return StepResult("deployment", True, None, {
            "deployment_digest": version["base_digest"], "deployment_version": digest,
            "source_digest": record["strategy_digest"]})
```

  `runner.py` and `mmr_cli.py` take `--deployment-version` (required with `--place-orders`, refused `DEPLOYMENT_VERSION_REQUIRED` otherwise) and pass it into `AcceptanceSettings`. Tests: `tests/sp1_acceptance/fakes.py` answers the two reads; `test_acceptance_run.py` seeds a version with `install_seeded_judgments` + `seed_judged_deployment` in its served fixture and passes `deployment_version`; `test_scenario_unit.py` updates the planned calls and the ENTER body keys.
- [ ] **Step 3: Run** `tests/sp1_acceptance tests/ai tests/automation tests/test_ai_paper_rpc.py tests/test_rpc_acl.py tests/test_strategy_ai_deployments.py` → pass.
- [ ] **Step 4: Full suite once, under the shared lock:**

```bash
until mkdir /private/tmp/mmr-suite.lock 2>/dev/null; do sleep 30; done
.venv/bin/python -m pytest tests/ -q -n 8 --timeout=120 --ignore=tests/test_ibrx_async.py \
  --basetemp=/private/tmp/sp2c-plan2-suite; status=$?
rmdir /private/tmp/mmr-suite.lock; exit $status
```

(Without pytest-xdist: drop `-n 8` and use `--timeout=60`.) Then `.venv/bin/python -m pytest tests/test_ibrx_async.py --timeout=30 -q`. Read the live summary; fix any failure caused by this plan before committing.
- [ ] **Step 5: Commit** `feat: run sp1 acceptance and the ai harness on judged deployment versions`.

## Self-review

- Spec 5.2 item 4: JUDGMENT_MISSING / NOT_DEPLOY / MISMATCH / ALREADY_BOUND, bundle signature + `require_qualified_research_evidence`, three-way binding, review names the judgment, cap, cooldown, bundle expiry, base then version in one transaction → Tasks 4–5. Item 5: table, domain, INITIAL/RENEWAL, one per judgment, digest rechecked on read, base row unchanged, both digests returned, `get_ai_deployment_version`, active definition, withdraw, non-DEPLOY renewal ends the line → Tasks 1, 3, 5, 6. Item 6 → Task 6. Item 8 → Tasks 3, 7, 8. Spec 5.4 → Task 9 (+ Task 10 pass-through). Spec 8 rows "registration", "bundle", "file changed", "expires/withdrawn/cools down after a signal" → Tasks 5, 4, 9, 8. Spec 9 "Registration bound", "Recheck", "Strategy binding", "Renewal version", "Access", "Bundle tampering" → Review Focus tests.
- Out of this plan: `get_deployment_forward_evidence` (Plan 1 port; Plan 5 fills it), `record_shadow_result` and shadow rows (Plan 3), the research cycle (Plan 4), the renewal request, the real `RenewalChecks` and the renewal-verdict read (Plan 5). The RENEWAL registration code stays here; Plan 5 wires it end to end.
- Known gap: no served-RPC test reaches a RESOLVED `register_ai_deployment` (a real bundle takes ~3 min). The in-process `test_registration_through_a_real_signed_bundle` covers the chain; Plan 4's acceptance covers the wire.
- Names checked against Plan 1 (`BacktestJudgments.get`, `load_case_verify_keys`, `load_verified_case`, `EvaluationCase` header, `cooling_until_in_tx`, `BacktestJudgeConfig`), Plan 3 (`trader.ai_deployment_versions.version_for_judgment`) and Plan 4 (`VersionReply`, registration body and outcome, `StrategyNode`).
