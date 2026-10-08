# AI Paper SP2c — Plan 5: Renewal — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** An expired AI deployment can be renewed. After a version expires, the `ai` controller asks the `research` service for a renewal case. The research service reads the version's forward evidence from the trader (its sealed shadow rows and its paper trips), signs a RENEWAL case by code, and opens no holdout and creates no trial. Jev rules on it. A renewal DEPLOY is registered on the same base deployment and the same bundle, while that bundle's attestation is still valid, and gets a fresh deployment version. Any other renewal verdict ends the line. When the bundle has expired, the renewal is refused; only a new evaluation with a new holdout can deploy that strategy again.

**Architecture:** Trader: one new module `trader/automation/deployment_renewal.py` holds the real ports Plan 1 left open: `VersionForwardEvidence` (Plan 1's `ForwardEvidenceSource`), `RenewalGate` (may this version be renewed now) and `TraderRenewalChecks` (Plan 1's `RenewalChecks`). One journal table, `renewal_judgments` (migration 125), keeps one renewal judgment per version and gives Plan 2's `renewals_of` its read. The shared, strict shape of the forward evidence lives in `trader/research/forward_evidence_view.py`, so the trader builds it and the research service parses it with the same model. Research: `trader/research/renewal_case.py` builds the RENEWAL case and `trader/research/renewal_service.py` serves the RENEWAL kind of `submit_evaluation` (no claim, no queue). Controller: Plan 4's `ResearchCycle` turns an EXPIRED version into a RENEWAL candidate (line `RENEWING`), judges it with the same Jev workflow, skips attestation, and registers the renewal with the line's stored registration body.

**Tech Stack:** Python 3.12, DuckDB (`SchemaMigrator`, `DuckDBConnection.transaction`), pydantic v2 strict models, Ed25519 (`AttestationSigner`, Plan 1's case verifier), `exchange_calendars` through `XNYSCalendarPolicy`, asyncio, pytest + pytest-asyncio. No new dependency.

**Spec:** `docs/superpowers/specs/2026-10-08-ai-paper-sp2c-backtest-judge-design.md`, binding sections 5.2 items 2, 5 and 7 (renewal parts), 6.2 "Renewal after expiry", 9 "Renewal version" and the renewal lines of 9 "Holdout discipline"; 11 (third open question). Index: `docs/superpowers/plans/2026-10-08-ai-paper-sp2c-00-index.md`.

## Global Constraints

- **Base:** master after SP2c Plans 1–4 are merged. Names are the merged ones from those plans (rule R0: the plan that builds the server side of a method owns its shape). If a merged name differs from this plan, change only this plan's code and say so in the commit body.
- **Migrations:** trader journal **125** (`renewal_judgments`); 126–129 stay free. `ai.duckdb`: none (Plan 4's tables already carry `kind`, `prior_version_digest` and `line_state = 'RENEWING'`); 40–49 stay free. Research DB: none (a renewal request is a `research_requests` row). No ALTER, no backfill (owner: no legacy data).
- **Principals and methods:** no new RPC method and no new allow-list row. `get_deployment_forward_evidence` stays `research` only (Plan 1); `submit_evaluation` stays `ai_research` only (Plan 3); `record_backtest_judgment` and `register_ai_deployment` stay `ai_research` only. Every handler keeps its own principal check.
- **Wire models:** `ConfigDict(extra="forbid", strict=True, frozen=True)`; floats are finite; digests match `^sha256:[0-9a-f]{64}$`; dates are `YYYY-MM-DD`.
- **Refusals:** a business refusal is a reply body with its own code, never a guess. A tampered shadow row or judgment is refused by name (`FORWARD_EVIDENCE_TAMPERED`, `JUDGMENT_TAMPERED`), never read as missing.
- **DuckDB:** only `DuckDBConnection.execute` / `.transaction`. File reads, signature checks and port calls run before the write transaction. Inside a transaction callback never call another store's `execute` or `transaction`.
- **Paper only.** The trader parts exist only when `ai_paper.enabled` built them (a live account refuses that stack). No IB order in any test.
- **Never print secrets.** Refusal details name files, digests and fields, never key bytes.
- Test-first. Per task run only the listed tests. Full suite once, in Task 9, under the shared lock.
- Commit subjects `feat:` / `test:` / `docs:`, lowercase, imperative. Every commit message ends with a blank line and `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.
- No container restart, deploy, push or GitHub post is authorized by this plan.

## Rulings (spec silent, or the code forces a choice)

1. **Renew only after expiry.** A renewal judgment is accepted only for a version whose status is `EXPIRED` (spec 6.2: "A DEPLOY that expires may be renewed"). `ACTIVE`, `NOT_STARTED` and `OVER_CAP` are `RENEWAL_NOT_DUE`; `WITHDRAWN` and `SUPERSEDED` are `RENEWAL_PRIOR_INVALID`; `JUDGMENT_ENDED` is `RENEWAL_LINE_ENDED`. So the forward window is always the whole version window `[first_session, expiry_session]`. Plan 2 ruling 7 still allows an early registration, but no early renewal judgment can exist to use it. *Cost if wrong:* one session without an active version between the old line and the new one (the version is EXPIRED from the day after its last session; the renewal is registered that evening; the new version starts the next session). *Owner to confirm.*
2. **A renewal takes no claim and no daily slot.** It is not an evaluation (spec 3: an evaluation names a frozen cohort to test); it runs no backtest, opens no holdout and writes no parameter trial (spec 6.2). Plan 1's RENEWAL case has no `request_id` and no `claim_day`, so it cannot be bound to a claim. *Cost if wrong:* a renewal could be asked without limit; in practice at most one per version (ruling 3). *Owner to confirm.*
3. **One renewal case and one renewal judgment per version.** The research request id is `"sha256:" + sha256_digest("mmr.research.renewal-request.v1", {"kind": "RENEWAL", "prior_version_digest": V})`: one case per version, a resend is `DUPLICATE`. The trader keeps at most one renewal judgment per version in `renewal_judgments` (migration 125, `prior_version` primary key), written in the judgment's own transaction (`RENEWAL_ALREADY_JUDGED`). Any non-DEPLOY renewal verdict, `NO_VERDICT` included, ends the line for good (Plan 2 ruling 6). *Cost if wrong:* a `NO_VERDICT` from a Jev outage ends a line; a new evaluation is the way back.
4. **The forward window and its rows.** Sessions = every XNYS session of `[first_session, expiry_session]`. Shadow rows are read by the version's own judgment id and session date, not by `shadow_results.deployment_version`, which is null for rows sent before registration (Plan 3 ruling 10). A session outside that judgment's shadow window (`shadow_window(decided_at, "DEPLOY", ...)`) can never get a row and is `NOT_REPLAYED`; it counts as incomplete. *Cost if wrong:* a version registered a day late (for example after `DEPLOY_CAP_REACHED`) cannot be renewed with DEPLOY. *Owner to confirm.*
5. **Missing rows wait, then count.** A session with no row is `MISSING`. The research service refuses the renewal as retryable `FORWARD_EVIDENCE_PENDING` while a missing session is younger than its close + `shadow_incomplete_after_hours` + 1 h (Plan 3 ruling 11: the replay writes an INCOMPLETE row by then). After that the case is signed with the session counted as incomplete. In the normal timing (renewal on the evening after the last session) every deadline has passed, so this only guards clock skew.
6. **The trader checks completeness itself.** A DEPLOY renewal needs every forward session `COMPLETE` by the trader's own sealed rows (`FORWARD_INCOMPLETE` otherwise), besides Plan 1's own `renewal_forward_complete(case)`. The case must name the version's exact binding (strategy key, file hash, params, conids, bar size; `artifact_id` and `family_id` when set) and the window's session count; any difference refuses the whole judgment (`RENEWAL_CASE_MISMATCH`).
7. **Bundle validity is the registration rule.** A DEPLOY renewal is blocked when `ResearchBundleCheck.check(evidence_ref, artifact_id=<line's artifact>, now)` fails (`BUNDLE_EXPIRED`, `BUNDLE_INVALID`, ...) or when `deployment_sessions(...)` leaves no session before the bundle's expiry (`BUNDLE_EXPIRED`). Plan 2's registrar applies the same two checks again.
8. **Refuse before Jev.** The forward evidence carries `renewable {ok, code, detail}`. The research service refuses a renewal (no case, no model call) while `ok` is false, when the key is off `strategy_allowlist` (`STRATEGY_NOT_ALLOWED`), or when the file on disk is not the deployed bytes (`STRATEGY_SOURCE_CHANGED`). A cooling-down strategy key cannot renew (`FAMILY_COOLING_DOWN`). The controller ends the line on any non-retryable refusal.
9. **Forward evidence shape (Plan 5 owns it).** Plan 1 left `evidence: dict|null`. Plan 5 defines it as `ForwardEvidenceView` (Cross-plan additions). Every shadow row is checked against its scoreboard seal on read (`FORWARD_EVIDENCE_TAMPERED`). Paper trips are the `round_trips` whose `decision_id` is an `ENTER` in `ai_paper_decisions` with this `deployment_version`; they are information for Jev, not a gate.
10. **No forward-performance threshold** (spec 11, third open question, default kept): complete forward data is the only code check; Jev judges the numbers. *Owner to confirm.*
11. **No attestation for a renewal.** The bundle's review names the line's INITIAL judgment (Plan 2 ruling 4); Plan 3's `attest_from_judgment` already refuses a RENEWAL judgment (`JUDGMENT_NOT_DEPLOY`). The controller registers the renewal with the line's stored registration body, only `judgment_id` replaced.
12. **The renewal case header.** `cohort = [selected_params]` = the deployed params; `artifact_id`, `family_id`, `selected_trial_id`, `eligibility_decision_digest` are copied from the line's INITIAL judgment (Plan 2's `binding_differences` then compares them with the bundle); `decision_state`, `ruleset_digest` and `holdout_passed` are null (Plan 1 ruling 15: a renewal opens no holdout); `final_rule_results` is empty; `stage` is `FORWARD_COMPLETE` only when every session is `COMPLETE`. The `evidence` carries the forward facts and the replay keys (`points: []`, `replay_index: 0`, `warmup_sessions`) and `holdout: null` (Plan 1 ruling 15: a RENEWAL case has holdout evidence absent or null, never a result), so the renewal judgment joins the shadow cohort like any judgment (spec 7) and its own forward rows exist for the next renewal.
13. **"The old version stays expired."** Plan 2 ruling 6 shows a renewed version as `SUPERSEDED` (wire `ENDED`) on purpose. The tests assert what matters: the old version is never active again and is never renewed again.
14. **Late binding in the trader wiring.** `BacktestJudgments` needs the renewal checks, and the checks need `DeploymentActivity`, which needs the judgments. `_build_ai_paper_parts` builds them in one function with two lambdas (`activity.status`, `backtest_judgments.get` / `.renewals_of`) that are only called after both objects exist.
15. **Controller line states.** `LIVE` → `RENEWING` (EXPIRED seen at a slot; one RENEWAL candidate per version, id `"rr-" + sha256("RENEWAL|" + V)[:32]`) → `ENDED` with `RENEWED`, `RENEWAL_<verdict>`, `RENEWAL_REFUSED_<code>`, `RENEWAL_JUDGMENT_<code>`, `RENEWAL_REGISTER_<code>`, or the version's own state (`WITHDRAWN`, `ENDED`). The renewal judgment id is `"jdg-" + sha256("RENEWAL|" + case_digest)[:32]`; INITIAL ids do not change.
16. **Jev's prompt** gains one sentence on RENEWAL cases. The facts are Plan 3's summary (`kind`, `stage`, `renewal_checks_passed`, `prior_version_digest`, `forward`); the menu is `FULL_MENU` only when `rules_passed` (= the forward window is complete).
17. **An INITIAL cap refusal never ends a renewal** (PR #91 thread 4218219455). A RENEWAL candidate takes no evaluation slot (ruling 2), so Plan 4's bulk close on `EVALUATION_LIMIT_REACHED` closes only INITIAL candidates (Plan 4 ruling 23). Only the renewal's own refusal or verdict ends its line. As a second guard, each slot reopens a `RENEWING` line's candidate that is `CLOSED` without any judgment row (state back to `NEW`, ERROR log naming the old end code); it reads no version for that (a `RENEWING` line is not read again).

## Cross-plan additions

Names Plan 5 adds or changes; other plans and later work use them exactly.

- **Journal migration 125** in `trader/automation/backtest_judge_schema.py`: `renewal_judgments (prior_version VARCHAR PRIMARY KEY, judgment_id VARCHAR NOT NULL UNIQUE, recorded_at TIMESTAMPTZ NOT NULL)`, applied by Plan 1's `apply_backtest_judge_migrations` (`RENEWAL_JUDGMENTS_MIGRATION_VERSION = 125`).
- **Plan 1 store:** `BacktestJudgments.renewals_of(version_digest) -> tuple[BacktestJudgment, ...]` (sealed reads; an index row without its judgment raises `JudgmentRefused("JUDGMENT_TAMPERED")`). New refusal code `RENEWAL_ALREADY_JUDGED`.
- **Plan 2 port:** `judgment_reader_for(judgments, *, cases_dir, verify_dir, renewals_of=None)`; the trader passes `renewals_of=backtest_judgments.renewals_of`.
- **Forward evidence** (`trader/research/forward_evidence_view.py`): `ForwardEvidenceView` = `{version_digest, base_digest, judgment_id, kind, prior_version_digest, status (Plan 2's raw status), first_session, expiry_session, binding: {strategy_key, strategy_path, class_name, strategy_file_hash, params, conids, bar_size, order_notional, bundle_digest}, line: {initial_judgment_id, family_id, selected_trial_id, artifact_id, eligibility_decision_digest}, renewable: {ok, code, detail}, sessions: [{session_date, state: COMPLETE|INCOMPLETE|MISSING|NOT_REPLAYED, reason, pnl_usd, fees_usd, trades, end_equity_usd}], trips: [{round_trip_id, conid, status, opened_session, closed_session, net_pnl_usd, fees_complete}], as_of}`. It is the `evidence` of `get_deployment_forward_evidence` (`status: "FOUND"`).
- **Trader codes** (forward evidence, renewal judgment): `DEPLOYMENT_VERSION_UNKNOWN`, `DEPLOYMENT_VERSION_TAMPERED`, `JUDGMENT_MISSING`, `JUDGMENT_TAMPERED`, `FORWARD_EVIDENCE_TAMPERED`, `RENEWAL_NOT_DUE`, `RENEWAL_PRIOR_INVALID`, `RENEWAL_LINE_ENDED`, `RENEWAL_ALREADY_JUDGED`, `RENEWAL_CASE_MISMATCH`, `FORWARD_INCOMPLETE`, `FAMILY_COOLING_DOWN`, Plan 2's bundle codes.
- **Trader module** `trader/automation/deployment_renewal.py`: `VersionFacts`, `VersionForwardEvidence(*, db, scoreboard, versions, deployments, status_of, judgment_of, gate, calendar, config, now)` with `read(version_digest) -> dict`, `facts(version_digest) -> VersionFacts`, `sessions(facts) -> list[ForwardSession]`; `RenewalGate(*, renewals_of, bundles, cooldowns, calendar, expiry_sessions)` with `line_refusal(version_digest, status)` and `deploy_block(base, line, now)`; `TraderRenewalChecks(*, forward, gate)`; `case_differences(case, facts, sessions) -> list[str]`. `_AiPaperParts.forward_evidence`.
- **Research:** `trader/research/renewal_case.py`: `RENEWAL_REQUEST_DOMAIN = "mmr.research.renewal-request.v1"`, `renewal_request_id(prior_version_digest)`, `forward_summary(view)`, `pending_sessions(view, *, now, incomplete_after_hours)`, `build_renewal_case(view, *, created_at, warmup_sessions)`. `trader/research/renewal_service.py`: `RenewalRequests(*, store, trader, signer, artifacts_root, repo_root, judge, warmup_sessions, incomplete_after_hours, now)` with `submit(prior_version_digest) -> dict`. `EvaluationService(..., renewals=None)`. `TraderPort.forward_evidence(version_digest) -> dict`. `ResearchStore.record_renewal(...)`. RENEWAL submit replies: `ACCEPTED` (state `DONE`), `DUPLICATE`, `REFUSED` with `TRADER_UNAVAILABLE` or `FORWARD_EVIDENCE_PENDING` (retryable), or `DEPLOYMENT_VERSION_UNKNOWN`, the `renewable` code, `STRATEGY_NOT_ALLOWED`, `STRATEGY_SOURCE_CHANGED` (not retryable). `evaluation_summary` of a RENEWAL case fills `renewal_checks_passed`, `prior_version_digest`, `forward` and the binding's `order_notional`.
- **Controller (Plan 4 names changed):** `judgment_id_for(case_digest, kind="INITIAL")`; `judgment_body` takes `kind` and `renewal_of_version` from the case summary; `renewal_candidate_id(prior_version_digest)`; `ResearchCycle._end_finished_lines(slot)`.

## Review Focus

1. **A renewal DEPLOY on the same bundle gets a fresh version; the old one never trades again; the strategy service loads a fresh instance; no attestation, holdout or trial is added.** → Task 3 `test_a_recorded_renewal_deploy_registers_a_fresh_version_on_the_same_base`; Task 7 `test_an_expired_line_is_renewed_on_the_same_bundle_without_attestation`; Task 8 `test_a_renewal_deploy_on_the_same_bundle_gets_a_fresh_version`.
2. **One renewal per version: the same renewal judgment or registration twice returns the same result; a second renewal judgment is refused.** → Task 1 `test_one_renewal_judgment_per_version_even_with_another_case`; Task 3 `test_one_renewal_judgment_per_version`, `test_a_recorded_renewal_deploy_registers_a_fresh_version_on_the_same_base`; Task 8 `test_a_renewal_deploy_on_the_same_bundle_gets_a_fresh_version`.
3. **After the bundle expires a renewal is refused: the research service asks no Jev, and the trader blocks a direct DEPLOY.** → Task 3 `test_a_renewal_after_the_bundle_expired_cannot_deploy`; Task 6 `test_a_renewal_that_cannot_be_renewed_is_refused_before_any_case`; Task 8 `test_a_renewal_after_the_bundle_expired_is_refused`.
4. **A missing, INCOMPLETE or not-replayed forward session never allows DEPLOY**, by the case's menu and by the trader's own rows. → Task 2 `test_a_session_outside_the_judgments_shadow_window_is_not_replayed`; Task 3 `test_a_missing_or_incomplete_forward_session_blocks_deploy_not_shadow`; Task 5 `test_any_session_not_complete_makes_a_forward_incomplete_case`; Task 8 `test_a_renewal_reject_cools_the_key_down_and_ends_the_line`.
5. **A non-DEPLOY renewal verdict ends the line at the trader and in the controller; a REJECT starts the cooldown; only an EXPIRED version can be renewed.** → Task 3 `test_a_renewal_reject_ends_the_line_and_cools_the_key_down`, `test_a_renewal_deploy_needs_an_expired_version_with_complete_forward_rows`; Task 7 `test_a_non_deploy_renewal_ends_the_line`; Task 8 `test_a_renewal_reject_cools_the_key_down_and_ends_the_line`.
6. **An INITIAL refused with the daily cap in the same cycle never closes the renewal; a renewal closed without a judgment is asked again at the next slot.** → Task 7 `test_an_initial_cap_refusal_leaves_the_renewal_eligible`, `test_a_renewing_line_closed_without_a_judgment_is_asked_again_at_the_next_slot`.

## File map

| File | Responsibility | Task |
|---|---|---|
| `trader/automation/backtest_judge_schema.py`, `trader/automation/backtest_judgments.py` | migration 125, one renewal judgment per version, `renewals_of` | 1 |
| `trader/research/forward_evidence_view.py`, `trader/automation/deployment_renewal.py`, `tests/automation/renewal_world.py` | forward evidence shape and source, renewal gate | 2 |
| `trader/automation/deployment_renewal.py` | `TraderRenewalChecks`, case binding | 3 |
| `trader/automation/ai_judgment_port.py`, `trader/trading/command_stack.py` | trader wiring | 4 |
| `trader/research/renewal_case.py`, `trader/research/case_builder.py` | renewal case builder, summary | 5 |
| `trader/research/renewal_service.py`, `trader/research/trader_port.py`, `trader/research/service_store.py`, `trader/research/evaluation_service.py`, `trader/research_service.py`, `tests/research/service_fakes.py` | RENEWAL kind of `submit_evaluation` | 6 |
| `trader/ai/backtest_judge.py`, `trader/ai/research_roles.py`, `trader/ai/research_cycle.py`, `tests/ai/research/cases.py` | renewal request and pump | 7 |
| `tests/ai/research/research_world.py`, `tests/ai/research/test_renewal_acceptance.py` | §9 renewal over signed RPC | 8 |
| `docs/OPERATIONAL_STATE.md`, `docs/ARCHITECTURE.md` | runbook, full suite | 9 |

---

### Task 1: One renewal judgment per version, and `renewals_of`

**Files:**
- Modify: `trader/automation/backtest_judge_schema.py` (migration 125), `trader/automation/backtest_judgments.py` (`_insert_in_tx`, `renewals_of`)
- Test: `tests/automation/test_renewal_judgments.py`

**Interfaces:**
- Consumes: Plan 1's `BacktestJudgments`, `BacktestJudgment`, `JudgmentRefused`, `RenewalStatus`; fixtures `world`, `judgment`, `renewal_case_body`, `count_judgments`, `VERSION`.
- Produces: `RENEWAL_JUDGMENTS_MIGRATION_VERSION = 125`; `BacktestJudgments.renewals_of(version_digest: str) -> tuple[BacktestJudgment, ...]`; refusal `RENEWAL_ALREADY_JUDGED`.

- [ ] **Step 1: Write the failing tests** (`tests/automation/test_renewal_judgments.py`)

```python
"""SP2c Plan 5 Task 1: at most one renewal judgment per deployment version (migration 125)."""
from __future__ import annotations

import pytest

from tests.automation.backtest_judge_fixtures import VERSION, count_judgments, judgment, renewal_case_body, world
from trader.automation.backtest_judgments import JudgmentRefused, RenewalStatus
from trader.research.evaluation_case import EvaluationCase, write_evaluation_case

OTHER = "sha256:" + "c" * 64


class AllowRenewals:
    """A renewal port that lets every RENEWAL through; Task 3 tests the real one."""

    def status(self, case, *, now):
        return RenewalStatus()


def renewal_case(w, prior=VERSION, created_at="2026-10-08T21:30:00+00:00"):
    raw = renewal_case_body(created_at=created_at)
    raw["renewal"] = {**raw["renewal"], "prior_deployment_version": prior}
    return write_evaluation_case(w.keys.cases_dir, EvaluationCase.model_validate(raw), w.keys.signer)


def renew(w, case, verdict="SHADOW", prior=VERSION, judgment_id="jdg-renewal-1"):
    return w.judgments.record(judgment(case, verdict, kind="RENEWAL", renewal_of_version=prior,
                                       judgment_id=judgment_id))


def test_one_renewal_judgment_per_version_even_with_another_case(tmp_path):               # review focus 2
    w = world(tmp_path, renewals=AllowRenewals())
    first = renewal_case(w)
    assert renew(w, first)["status"] == "RECORDED"
    assert renew(w, first)["status"] == "EXISTING"                                       # an exact retry
    second = renewal_case(w, created_at="2026-10-08T21:31:00+00:00")                      # another case, same version
    refused = renew(w, second, "REJECT", judgment_id="jdg-renewal-2")
    assert (refused["status"], refused["code"]) == ("REFUSED", "RENEWAL_ALREADY_JUDGED")
    assert count_judgments(w.db) == 1
    assert w.db.execute("SELECT prior_version, judgment_id FROM renewal_judgments", fetch="all") == [
        (VERSION, "jdg-renewal-1")]


def test_renewals_of_reads_the_sealed_judgments_of_one_version(tmp_path):
    w = world(tmp_path, renewals=AllowRenewals())
    renew(w, renewal_case(w), "REJECT")
    renew(w, renewal_case(w, prior=OTHER), "DEPLOY", prior=OTHER, judgment_id="jdg-renewal-9")
    (found,) = w.judgments.renewals_of(VERSION)
    assert (found.judgment_id, found.kind, found.verdict) == ("jdg-renewal-1", "RENEWAL", "REJECT")
    assert found.cooldown_until_session is not None                                       # a renewal REJECT cools down
    assert w.judgments.renewals_of("sha256:" + "d" * 64) == ()


def test_an_index_row_without_its_judgment_is_tampering(tmp_path):
    w = world(tmp_path, renewals=AllowRenewals())
    w.db.execute("INSERT INTO renewal_judgments VALUES (?, 'jdg-ghost-01', now())", [VERSION])
    with pytest.raises(JudgmentRefused) as exc:
        w.judgments.renewals_of(VERSION)
    assert exc.value.code == "JUDGMENT_TAMPERED"


def test_an_initial_judgment_writes_no_renewal_index_row(tmp_path):
    from tests.automation.backtest_judge_fixtures import finished
    w = world(tmp_path)
    w.judgments.record(judgment(finished(w), "SHADOW"))
    assert w.db.execute("SELECT COUNT(*) FROM renewal_judgments", fetch="one")[0] == 0
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/automation/test_renewal_judgments.py -q`
Expected: FAIL (`Catalog Error: Table with name renewal_judgments does not exist`, and `AttributeError: 'BacktestJudgments' object has no attribute 'renewals_of'`).

- [ ] **Step 3: Add migration 125** (`trader/automation/backtest_judge_schema.py`)

Change the module docstring to `"""Journal tables of the SP2c backtest judge (migrations 110, 111 and Plan 5's 125)."""` and add:

```python
RENEWAL_JUDGMENTS_MIGRATION_VERSION = 125

_RENEWAL_JUDGMENTS = """CREATE TABLE IF NOT EXISTS renewal_judgments (
    prior_version VARCHAR PRIMARY KEY, judgment_id VARCHAR NOT NULL UNIQUE,
    recorded_at TIMESTAMPTZ NOT NULL)"""
```

and, at the end of `apply_backtest_judge_migrations` before `return applied`:

```python
    if migrator.apply(RENEWAL_JUDGMENTS_MIGRATION_VERSION, "sp2c_renewal_judgments", (_RENEWAL_JUDGMENTS,)):
        applied.append(RENEWAL_JUDGMENTS_MIGRATION_VERSION)
```

- [ ] **Step 4: Enforce one renewal judgment per version** (`trader/automation/backtest_judgments.py`)

In `_insert_in_tx`, the INITIAL block gains an `else` branch (the judgment row is inserted after it, in the same transaction, so a refusal rolls both back):

```python
        if judgment.kind == "INITIAL":
            other = conn.execute("SELECT judgment_id FROM backtest_judgments WHERE request_id = ?",
                                 [judgment.request_id]).fetchone()
            if other is not None:
                raise JudgmentRefused(JUDGMENT_CONFLICT, f"the evaluation is already judged by {other[0]}")
            self._check_claim_in_tx(conn, case, decided_at)
        else:
            self._claim_renewal_in_tx(conn, judgment)
```

Add to `BacktestJudgments`:

```python
    def renewals_of(self, version_digest: str) -> tuple[BacktestJudgment, ...]:
        """The RENEWAL judgment naming ``version_digest`` (at most one, migration 125). Each read is sealed."""
        rows = self._db.execute("SELECT judgment_id FROM renewal_judgments WHERE prior_version = ?",
                                [version_digest], fetch="all")
        found = []
        for (judgment_id,) in rows:
            judgment = self.get(judgment_id)
            if judgment is None or judgment.kind != "RENEWAL" \
                    or judgment.binding["prior_deployment_version"] != version_digest:
                raise JudgmentRefused("JUDGMENT_TAMPERED",
                                      f"the renewal index names {judgment_id} for {version_digest}")
            found.append(judgment)
        return tuple(found)

    @staticmethod
    def _claim_renewal_in_tx(conn: Any, judgment: BacktestJudgment) -> None:
        """SP2c Plan 5 ruling 3: one renewal judgment per version, whatever its case."""
        prior = judgment.binding["prior_deployment_version"]
        other = conn.execute("SELECT judgment_id FROM renewal_judgments WHERE prior_version = ?",
                             [prior]).fetchone()
        if other is not None:
            raise JudgmentRefused("RENEWAL_ALREADY_JUDGED", f"version {prior} is already judged by {other[0]}")
        conn.execute("INSERT INTO renewal_judgments (prior_version, judgment_id, recorded_at) VALUES (?, ?, ?)",
                     [prior, judgment.judgment_id, judgment.recorded_at])
```

Add `RENEWAL_ALREADY_JUDGED` to the module docstring's list of refusals.

- [ ] **Step 5: Run the tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/automation/test_renewal_judgments.py tests/automation/test_backtest_judgments.py tests/automation/test_evaluation_claims.py -q`
Expected: PASS (Plan 1's `test_a_renewal_waits_for_plan_5` still passes: the default port refuses before any write).

- [ ] **Step 6: Commit**

```bash
git add trader/automation/backtest_judge_schema.py trader/automation/backtest_judgments.py \
  tests/automation/test_renewal_judgments.py
git commit -m "$(cat <<'EOF'
feat: keep one renewal judgment per deployment version

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 2: The forward evidence of a deployment version

**Files:**
- Create: `trader/research/forward_evidence_view.py`, `trader/automation/deployment_renewal.py` (first half), `tests/automation/renewal_world.py`
- Test: `tests/automation/test_version_forward_evidence.py`

**Interfaces:**
- Consumes: Plan 2's `AiDeploymentVersionStore.get`, `.withdraw`, `AiDeploymentStore.get_sealed`, `DeploymentActivity.status`, `deployment_sessions`, `BundleRefused`, `DeploymentRefused`, `strategy_key`, `judgment_reader_for`, `cooldown_reader_for`; Plan 1's `BacktestJudgments.get`, `ForwardEvidenceRefused`; Plan 3's `shadow_window`, `ShadowIngest`, `RecordShadowResultRequest`, migration 120; scoreboard `ScoreboardStore.fetch`, `row_digest`.
- Produces: `ForwardEvidenceView` and its parts (Cross-plan additions); `VersionFacts`; `VersionForwardEvidence`; `RenewalGate`; test world `renewal_world(tmp_path, *, bundles=None) -> RenewalWorld`.

- [ ] **Step 1: Write the shared view** (`trader/research/forward_evidence_view.py`)

```python
"""The forward evidence of one deployment version (SP2c spec 5.2 item 7, 6.2 "Renewal after expiry").

The trader builds it for get_deployment_forward_evidence; the research service parses it with this same
strict model and signs a renewal case from it. Every value is code-computed."""
from __future__ import annotations

from typing import Annotated, Literal, Optional, Union

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, StrictStr

DIGEST = r"^sha256:[0-9a-f]{64}$"
DAY = r"^\d{4}-\d{2}-\d{2}$"
Money = Annotated[float, Field(strict=True, allow_inf_nan=False)]
Scalar = Union[StrictBool, StrictInt, Money, StrictStr]
VERSION_STATUSES = ("ACTIVE", "NOT_STARTED", "EXPIRED", "WITHDRAWN", "SUPERSEDED", "JUDGMENT_ENDED", "OVER_CAP")


class _View(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class VersionBinding(_View):
    strategy_key: StrictStr
    strategy_path: StrictStr
    class_name: StrictStr
    strategy_file_hash: StrictStr = Field(pattern=DIGEST)
    params: dict[StrictStr, Scalar]
    conids: list[StrictInt] = Field(min_length=1, max_length=20)
    bar_size: StrictStr
    order_notional: Money = Field(gt=0)
    bundle_digest: StrictStr = Field(pattern=DIGEST)


class LineFacts(_View):
    """The INITIAL judgment of the line: the evaluation the bundle attests."""
    initial_judgment_id: StrictStr
    family_id: Optional[StrictStr]
    selected_trial_id: Optional[StrictStr]
    artifact_id: StrictStr
    eligibility_decision_digest: Optional[StrictStr]


class Renewability(_View):
    ok: StrictBool
    code: Optional[StrictStr]
    detail: StrictStr


class ForwardSession(_View):
    session_date: StrictStr = Field(pattern=DAY)
    state: Literal["COMPLETE", "INCOMPLETE", "MISSING", "NOT_REPLAYED"]
    reason: Optional[StrictStr]
    pnl_usd: Optional[Money]
    fees_usd: Optional[Money]
    trades: Optional[StrictInt]
    end_equity_usd: Optional[Money]


class PaperTrip(_View):
    round_trip_id: StrictStr
    conid: StrictInt
    status: Literal["OPEN", "CLOSED"]
    opened_session: StrictStr = Field(pattern=DAY)
    closed_session: Optional[StrictStr]
    net_pnl_usd: Optional[Money]
    fees_complete: StrictBool


class ForwardEvidenceView(_View):
    version_digest: StrictStr = Field(pattern=DIGEST)
    base_digest: StrictStr = Field(pattern=DIGEST)
    judgment_id: StrictStr
    kind: Literal["INITIAL", "RENEWAL"]
    prior_version_digest: Optional[StrictStr]
    status: Literal[VERSION_STATUSES]
    first_session: StrictStr = Field(pattern=DAY)
    expiry_session: StrictStr = Field(pattern=DAY)
    binding: VersionBinding
    line: LineFacts
    renewable: Renewability
    sessions: list[ForwardSession]
    trips: list[PaperTrip]
    as_of: StrictStr

    def to_wire(self) -> dict:
        return self.model_dump(mode="json")
```

- [ ] **Step 2: Write the test world** (`tests/automation/renewal_world.py`)

```python
"""Tests only: one judged DEPLOY line on a real journal, the real renewal ports and a fake bundle check."""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any

from tests.automation.backtest_judge_fixtures import (
    FILE_HASH, NOW, World, finished, judge_config, judgment, renewal_case_body, world,
)
from trader.automation.ai_bundle_check import BundleFacts, BundleRefused
from trader.automation.ai_deployment_activity import DeploymentActivity
from trader.automation.ai_deployment_registration import AiDeploymentRegistrar
from trader.automation.ai_deployment_versions import (
    INITIAL, AiDeploymentVersionStore, DeploymentVersion, apply_ai_deployment_version_migrations,
)
from trader.automation.ai_deployments import (
    AiDeployment, AiDeploymentStore, apply_ai_deployment_migration, deployment_digest,
)
from trader.automation.ai_judgment_port import cooldown_reader_for, judgment_reader_for
from trader.automation.ai_paper_decision import apply_ai_paper_decision_migration
from trader.automation.backtest_judgments import BacktestJudgments
from trader.automation.calendar_policy import XNYSCalendarPolicy
from trader.automation.deployment_renewal import RenewalGate, TraderRenewalChecks, VersionForwardEvidence
from trader.data.schema_migrations import SchemaMigrator
from trader.research.evaluation_case import EvaluationCase, write_evaluation_case
from trader.scoreboard.schema import apply_scoreboard_migrations
from trader.scoreboard.shadow_ingest import RecordShadowResultRequest, ShadowIngest
from trader.scoreboard.store import ScoreboardStore

UTC = dt.timezone.utc
BUNDLE = "sha256:" + "e" * 64
FIRST, EXPIRY = dt.date(2026, 10, 9), dt.date(2026, 10, 13)       # three sessions: 10-09, 10-12, 10-13
SESSIONS = ("2026-10-09", "2026-10-12", "2026-10-13")
AFTER_EXPIRY = dt.datetime(2026, 10, 14, 22, 0, tzinfo=UTC)          # Wednesday 18:00 New York
RESEARCH = SimpleNamespace(principal="research")
RECORD = {"strategy_path": "strategies/opening_range_breakout.py", "strategy_digest": FILE_HASH,
          "class_name": "OpeningRangeBreakout", "params": {"RANGE_MINUTES": 15}, "conids": [265598, 272093],
          "bar_size": "5 mins", "style": "intraday_long", "decider": "jev", "decider_verdict": "DEPLOY",
          "evidence_ref": BUNDLE, "evidence_order_notional": 1900.0}


class FakeBundles:
    """ResearchBundleCheck's contract: artifact art-1 of the fixture line, attested until ``expires_at``."""

    def __init__(self, expires_at: dt.datetime = dt.datetime(2026, 12, 31, tzinfo=UTC)):
        self.expires_at = expires_at

    def manifest_artifact_id(self, digest: str) -> str:
        return "art-1"

    def check(self, digest: str, *, artifact_id: str, now: dt.datetime) -> BundleFacts:
        if digest != BUNDLE or artifact_id != "art-1":
            raise BundleRefused("BUNDLE_INVALID", f"{digest} does not attest {artifact_id}")
        if now >= self.expires_at:
            raise BundleRefused("BUNDLE_EXPIRED", f"the attestation expired at {self.expires_at.isoformat()}")
        return BundleFacts(BUNDLE, "art-1", "fam-1", RECORD["strategy_path"], RECORD["class_name"], FILE_HASH,
                           dict(RECORD["params"]), tuple(RECORD["conids"]), RECORD["bar_size"], 1900.0,
                           "openrouter/jev-1#jdg-00000001", "llm", self.expires_at)


@dataclass
class RenewalWorld:
    base: World
    judgments: BacktestJudgments
    reader: Any
    versions: AiDeploymentVersionStore
    deployments: AiDeploymentStore
    activity: DeploymentActivity
    forward: VersionForwardEvidence
    bundles: FakeBundles
    shadow: ShadowIngest
    scoreboard: ScoreboardStore

    @property
    def clock(self):
        return self.base.clock

    def deploy_initial(self, *, first: dt.date = FIRST, expiry: dt.date = EXPIRY) -> str:
        """The INITIAL line: Plan 1's recorded DEPLOY and a sealed version (Plan 2's stores, no bundle path)."""
        assert self.judgments.record(judgment(finished(self.base), "DEPLOY"))["status"] == "RECORDED"
        deployment = AiDeployment.from_json(RECORD)
        version = DeploymentVersion(deployment_digest(deployment), "jdg-00000001", INITIAL, None, first, expiry)

        def write(conn):
            self.deployments.register_in_tx(conn, deployment, principal="ai_research", command_id="c-initial")
            return self.versions.seal_in_tx(conn, version, request_digest="sha256:" + "1" * 64,
                                            principal="ai_research", command_id="c-initial")[0]
        return self.base.db.transaction(write)

    def shadow_row(self, session: str, status: str = "COMPLETE", judgment_id: str = "jdg-00000001") -> str:
        """One row through Plan 3's real ingest (window check, seal)."""
        stored = self.judgments.get(judgment_id)
        numbers = ({"reason": None, "pnl_usd": 4.0, "fees_usd": 1.0, "trades": 1, "end_equity_usd": 100_004.0}
                   if status == "COMPLETE" else
                   {"reason": "BARS_MISSING: fixture", "pnl_usd": None, "fees_usd": None, "trades": None,
                    "end_equity_usd": None})
        request = RecordShadowResultRequest(judgment_id=judgment_id, case_digest=stored.case_digest,
                                            verdict=stored.verdict, session_date=session, status=status,
                                            bar_size="5 mins", **numbers)
        return self.shadow.record(request, RESEARCH)["status"]

    def paper_trip(self, version_digest: str, round_trip_id: str, *, net_pnl: float) -> None:
        """An ENTER decision bound to ``version_digest`` and the round trip it opened."""
        self.base.db.execute(
            "INSERT INTO ai_paper_decisions (command_id, decision_id, account_id, conid, action, body_json, state, "
            "received_at, updated_at, deployment_version) VALUES (?, ?, 'DU1', 265598, 'ENTER', '{}', 'FINAL', ?, ?, ?)",
            [f"cmd-{round_trip_id}", f"dec-{round_trip_id}", NOW, NOW, version_digest])
        self.scoreboard.replace_round_trips(f"exp-{round_trip_id}", [{
            "round_trip_id": round_trip_id, "experiment_id": f"exp-{round_trip_id}", "account_id": "DU1",
            "conid": 265598, "symbol": "AAPL", "direction": "LONG", "status": "CLOSED", "opened_at": NOW,
            "closed_at": NOW, "opened_session": FIRST, "closed_session": FIRST, "entry_qty": 10.0, "exit_qty": 10.0,
            "entry_avg": 100.0, "exit_avg": 101.0, "gross_pnl_usd": net_pnl + 1.0, "fees_usd": 1.0,
            "net_pnl_usd": net_pnl, "fees_complete": True, "notional_traded_usd": 2010.0, "strategy_version": None,
            "decider": "jev", "policy_revision": None, "style": "intraday_long",
            "decision_id": f"dec-{round_trip_id}", "links_digest": None, "exec_ids": "[]", "fills_digest": "f"}])

    def renewal_case(self, prior: str, *, forward_sessions: int = 3, incomplete_sessions: int = 0,
                     **changes) -> str:
        raw = renewal_case_body(**changes)
        raw["renewal"] = {"prior_deployment_version": prior, "forward_sessions": forward_sessions,
                          "incomplete_sessions": incomplete_sessions}
        return write_evaluation_case(self.base.keys.cases_dir, EvaluationCase.model_validate(raw),
                                     self.base.keys.signer)

    def renew(self, case_digest: str, prior: str, verdict: str = "DEPLOY", *,
              judgment_id: str = "jdg-renewal-1", **changes) -> dict:
        request = judgment(case_digest, verdict, kind="RENEWAL", renewal_of_version=prior, judgment_id=judgment_id,
                           decided_at=self.clock.now.isoformat(), **changes)
        return self.judgments.record(request)

    def registrar(self) -> AiDeploymentRegistrar:
        return AiDeploymentRegistrar(
            db=self.base.db, deployments=self.deployments, versions=self.versions, activity=self.activity,
            judgments=self.reader, cooldowns=cooldown_reader_for(self.base.db), bundles=self.bundles,
            calendar=XNYSCalendarPolicy(), expiry_sessions=3, now=self.clock)


def renewal_world(tmp_path, *, bundles: FakeBundles | None = None) -> RenewalWorld:
    base = world(tmp_path, deploy_expiry_sessions=3)
    migrator = SchemaMigrator(base.db)
    apply_ai_deployment_migration(migrator)
    apply_ai_deployment_version_migrations(migrator)
    apply_ai_paper_decision_migration(migrator)
    apply_scoreboard_migrations(migrator)
    config, calendar, bundles = judge_config(deploy_expiry_sessions=3), XNYSCalendarPolicy(), bundles or FakeBundles()
    scoreboard = ScoreboardStore(base.db, now=base.clock)
    deployments = AiDeploymentStore(base.db, now=base.clock)
    versions = AiDeploymentVersionStore(base.db, now=base.clock)
    cooldowns = cooldown_reader_for(base.db)
    # Plan 5 ruling 14: the lambdas run only after `judgments` and `activity` exist.
    gate = RenewalGate(renewals_of=lambda digest: judgments.renewals_of(digest), bundles=bundles,
                       cooldowns=cooldowns, calendar=calendar, expiry_sessions=3)
    forward = VersionForwardEvidence(db=base.db, scoreboard=scoreboard, versions=versions, deployments=deployments,
                                     status_of=lambda digest: activity.status(digest),
                                     judgment_of=lambda judgment_id: judgments.get(judgment_id), gate=gate,
                                     calendar=calendar, config=config, now=base.clock)
    judgments = BacktestJudgments(base.db, config=config, calendar=calendar, cases_dir=base.keys.cases_dir,
                                  verify_dir=base.keys.verify_dir, now=base.clock,
                                  renewals=TraderRenewalChecks(forward=forward, gate=gate))
    reader = judgment_reader_for(judgments, cases_dir=base.keys.cases_dir, verify_dir=base.keys.verify_dir,
                                 renewals_of=judgments.renewals_of)
    activity = DeploymentActivity(versions=versions, deployments=deployments, judgments=reader,
                                  cooldowns=cooldowns, max_active=3, now=base.clock)
    shadow = ShadowIngest(store=scoreboard, judgments=judgments, versions=versions, config=config, now=base.clock)
    return RenewalWorld(base, judgments, reader, versions, deployments, activity, forward, bundles, shadow, scoreboard)
```

(`TraderRenewalChecks` comes in Task 3; until then this import fails, so Task 2 and Task 3 land together in one commit. `judgment_reader_for(..., renewals_of=...)` is Task 4 Step 3; do that one-line change now, in this task, and Task 4 only wires it.)

- [ ] **Step 3: Write the failing tests** (`tests/automation/test_version_forward_evidence.py`)

```python
"""SP2c Plan 5 Task 2: get_deployment_forward_evidence over the real version, shadow rows and trips."""
from __future__ import annotations

import datetime as dt

import pytest

from tests.automation.backtest_judge_fixtures import KEY, insert_reject
from tests.automation.renewal_world import (
    AFTER_EXPIRY, BUNDLE, SESSIONS, UTC, FakeBundles, renewal_world,
)
from trader.automation.forward_evidence import ForwardEvidenceRefused
from trader.research.forward_evidence_view import ForwardEvidenceView


def test_the_forward_evidence_lists_every_session_of_the_version(tmp_path):
    rw = renewal_world(tmp_path)
    v1 = rw.deploy_initial()
    assert rw.shadow_row(SESSIONS[0]) == "INSERTED"
    assert rw.shadow_row(SESSIONS[1], status="INCOMPLETE") == "INSERTED"
    rw.clock.now = AFTER_EXPIRY
    view = ForwardEvidenceView.model_validate(rw.forward.read(v1))
    assert [(s.session_date, s.state) for s in view.sessions] == [
        ("2026-10-09", "COMPLETE"), ("2026-10-12", "INCOMPLETE"), ("2026-10-13", "MISSING")]
    assert view.sessions[0].pnl_usd == 4.0 and view.sessions[1].reason == "BARS_MISSING: fixture"
    assert (view.status, view.kind, view.prior_version_digest) == ("EXPIRED", "INITIAL", None)
    assert (view.binding.bundle_digest, view.binding.strategy_key, view.binding.conids) == (
        BUNDLE, KEY, [265598, 272093])
    assert (view.line.initial_judgment_id, view.line.artifact_id, view.line.family_id) == (
        "jdg-00000001", "art-1", "fam-1")
    assert (view.renewable.ok, view.trips) == (True, [])


def test_a_session_outside_the_judgments_shadow_window_is_not_replayed(tmp_path):          # review focus 4
    rw = renewal_world(tmp_path)
    v1 = rw.deploy_initial(first=dt.date(2026, 10, 12), expiry=dt.date(2026, 10, 14))       # registered a day late
    rw.clock.now = dt.datetime(2026, 10, 15, 22, 0, tzinfo=UTC)
    assert [s["state"] for s in rw.forward.read(v1)["sessions"]] == ["MISSING", "MISSING", "NOT_REPLAYED"]


def test_paper_trips_of_the_version_and_only_of_it(tmp_path):
    rw = renewal_world(tmp_path)
    v1 = rw.deploy_initial()
    rw.paper_trip(v1, "rt-1", net_pnl=12.5)
    rw.paper_trip("sha256:" + "9" * 64, "rt-2", net_pnl=-3.0)
    trips = rw.forward.read(v1)["trips"]
    assert [(t["round_trip_id"], t["net_pnl_usd"], t["status"], t["fees_complete"]) for t in trips] == [
        ("rt-1", 12.5, "CLOSED", True)]


@pytest.mark.parametrize("setup,code", [
    ("active", "RENEWAL_NOT_DUE"), ("withdrawn", "RENEWAL_PRIOR_INVALID"), ("bundle_expired", "BUNDLE_EXPIRED"),
    ("bundle_ends_before_the_next_session", "BUNDLE_EXPIRED"), ("cooling", "FAMILY_COOLING_DOWN")])
def test_the_forward_evidence_says_why_a_version_cannot_be_renewed(tmp_path, setup, code):
    expires = {"bundle_expired": dt.datetime(2026, 10, 14, 12, tzinfo=UTC),
               "bundle_ends_before_the_next_session": dt.datetime(2026, 10, 15, 12, tzinfo=UTC)}
    rw = renewal_world(tmp_path, bundles=FakeBundles(expires[setup]) if setup in expires else None)
    v1 = rw.deploy_initial()
    rw.clock.now = dt.datetime(2026, 10, 12, 15, 0, tzinfo=UTC) if setup == "active" else AFTER_EXPIRY
    if setup == "withdrawn":
        rw.versions.withdraw(v1, reason="operator", principal="cli", command_id="w1")
    if setup == "cooling":
        insert_reject(rw.base.db, KEY, dt.date(2026, 10, 30))
    renewable = rw.forward.read(v1)["renewable"]
    assert (renewable["ok"], renewable["code"]) == (False, code)


def test_tampered_rows_and_unknown_versions_are_refused_by_name(tmp_path):
    rw = renewal_world(tmp_path)
    v1 = rw.deploy_initial()
    rw.shadow_row(SESSIONS[0])
    rw.base.db.execute("UPDATE shadow_results SET pnl_usd = 400.0")
    with pytest.raises(ForwardEvidenceRefused) as exc:
        rw.forward.read(v1)
    assert exc.value.code == "FORWARD_EVIDENCE_TAMPERED"
    with pytest.raises(ForwardEvidenceRefused) as exc:
        rw.forward.read("sha256:" + "f" * 64)
    assert exc.value.code == "DEPLOYMENT_VERSION_UNKNOWN"
```

- [ ] **Step 4: Run the tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/automation/test_version_forward_evidence.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'trader.automation.deployment_renewal'`.

- [ ] **Step 5: Implement the first half of** `trader/automation/deployment_renewal.py`

```python
"""Renewal of an expired AI deployment version (SP2c spec 5.2 items 2 and 7, 6.2 "Renewal after expiry").

VersionForwardEvidence answers get_deployment_forward_evidence: the version's sessions, its sealed shadow rows
and its paper trips. RenewalGate says whether a version may be renewed now. TraderRenewalChecks (Task 3) is
Plan 1's RenewalChecks port. Every read happens before any write transaction.
"""
from __future__ import annotations

import datetime as dt
import hmac
from dataclasses import dataclass
from typing import Any, Callable, Optional

from trader.automation.ai_bundle_check import BundleRefused
from trader.automation.ai_deployment_activity import deployment_sessions
from trader.automation.ai_deployments import AiDeployment, DeploymentRefused
from trader.automation.ai_judgment_port import strategy_key
from trader.automation.backtest_judgments import JudgmentRefused
from trader.automation.forward_evidence import ForwardEvidenceRefused
from trader.research.forward_evidence_view import (
    ForwardEvidenceView, ForwardSession, LineFacts, PaperTrip, Renewability, VersionBinding,
)
from trader.research.shadow_window import shadow_window
from trader.scoreboard.seal import row_digest

NOT_DUE = frozenset({"ACTIVE", "NOT_STARTED", "OVER_CAP"})
Refusal = tuple[str, str]


def _money(value: Any) -> Optional[float]:
    return None if value is None else float(value)


@dataclass(frozen=True)
class VersionFacts:
    digest: str
    version: Any                 # DeploymentVersion
    status: str                  # Plan 2's raw status
    base: AiDeployment
    line: LineFacts
    decided_at: dt.datetime      # of the version's own judgment: it fixes the shadow window (spec 7)


class RenewalGate:
    """May this version be renewed now? One answer for the forward evidence and for the judgment check."""

    def __init__(self, *, renewals_of: Callable[[str], tuple], bundles: Any, cooldowns: Any, calendar: Any,
                 expiry_sessions: int):
        self._renewals_of, self._bundles, self._cooldowns = renewals_of, bundles, cooldowns
        self._calendar, self._expiry_sessions = calendar, expiry_sessions

    def line_refusal(self, version_digest: str, status: str) -> Optional[Refusal]:
        """Ruling 1: only an EXPIRED version, never renewed or judged for renewal before."""
        if status in NOT_DUE:
            return "RENEWAL_NOT_DUE", f"the version is {status}; a renewal starts after its expiry"
        if status == "WITHDRAWN":
            return "RENEWAL_PRIOR_INVALID", "the version was withdrawn"
        if status == "SUPERSEDED":
            return "RENEWAL_PRIOR_INVALID", "the version was already renewed"
        if status == "JUDGMENT_ENDED":
            return "RENEWAL_LINE_ENDED", "a judgment ended this line"
        if self._renewals_of(version_digest):
            return "RENEWAL_ALREADY_JUDGED", "a renewal judgment already names this version"
        return None

    def deploy_block(self, base: AiDeployment, line: LineFacts, now: dt.datetime) -> Optional[Refusal]:
        """Rulings 7 and 8: the registration's own bundle and session rule, and no cooldown."""
        try:
            bundle = self._bundles.check(base.evidence_ref, artifact_id=line.artifact_id, now=now)
            deployment_sessions(self._calendar, registered_at=now, sessions=self._expiry_sessions,
                                bundle_expires_at=bundle.expires_at)
        except BundleRefused as refused:
            return refused.code, refused.message
        except DeploymentRefused as refused:            # BUNDLE_EXPIRED: no session left before the expiry
            return refused.code, refused.message
        if self._cooldowns.cooling_down(strategy_key(base.strategy_path, base.class_name), now):
            return "FAMILY_COOLING_DOWN", "the strategy key is cooling down"
        return None


class VersionForwardEvidence:
    """Plan 1's ForwardEvidenceSource over Plan 2's versions and Plan 3's sealed shadow rows (ruling 4)."""

    def __init__(self, *, db: Any, scoreboard: Any, versions: Any, deployments: Any,
                 status_of: Callable[[str], str], judgment_of: Callable[[str], Any], gate: RenewalGate,
                 calendar: Any, config: Any, now: Callable[[], dt.datetime]):
        self._db, self._scoreboard, self._versions, self._deployments = db, scoreboard, versions, deployments
        self._status_of, self._judgment_of, self._gate = status_of, judgment_of, gate
        self._calendar, self._config, self._now = calendar, config, now

    def read(self, version_digest: str) -> dict:
        facts = self.facts(version_digest)
        now = self._now()
        refusal = self._gate.line_refusal(version_digest, facts.status) \
            or self._gate.deploy_block(facts.base, facts.line, now)
        version = facts.version
        return ForwardEvidenceView(
            version_digest=version_digest, base_digest=version.base_digest, judgment_id=version.judgment_id,
            kind=version.kind, prior_version_digest=version.prior_version, status=facts.status,
            first_session=version.first_session.isoformat(), expiry_session=version.expiry_session.isoformat(),
            binding=self._binding(facts.base), line=facts.line,
            renewable=Renewability(ok=refusal is None, code=None if refusal is None else refusal[0],
                                   detail="" if refusal is None else refusal[1]),
            sessions=self.sessions(facts), trips=self._trips(version_digest), as_of=now.isoformat()).to_wire()

    def facts(self, version_digest: str) -> VersionFacts:
        try:
            version = self._versions.get(version_digest)
            status = self._status_of(version_digest)
            base = self._deployments.get_sealed(version.base_digest)
            first = version
            while first.prior_version is not None:            # the INITIAL version of the line
                first = self._versions.get(first.prior_version)
        except DeploymentRefused as refused:
            raise ForwardEvidenceRefused(refused.code, refused.message) from None
        own, initial = self._judgment(version.judgment_id), self._judgment(first.judgment_id)
        binding = initial.binding
        line = LineFacts(initial_judgment_id=initial.judgment_id, family_id=binding["family_id"],
                         selected_trial_id=binding["selected_trial_id"], artifact_id=binding["artifact_id"],
                         eligibility_decision_digest=binding["eligibility_decision_digest"])
        return VersionFacts(version_digest, version, status, base, line,
                            dt.datetime.fromisoformat(own.body["decided_at"]))

    def sessions(self, facts: VersionFacts) -> list[ForwardSession]:
        version = facts.version
        window_first, window_last = shadow_window(
            facts.decided_at, "DEPLOY", deploy_expiry_sessions=self._config.deploy_expiry_sessions,
            family_cooldown_sessions=self._config.family_cooldown_sessions)
        rows = self._sealed_rows(version.judgment_id)
        found = []
        for day in self._calendar.sessions_in_range(version.first_session, version.expiry_session):
            row = rows.get(day)
            if not window_first <= day <= window_last:
                found.append(_session(day, "NOT_REPLAYED", "outside the judgment's shadow window"))
            elif row is None:
                found.append(_session(day, "MISSING", None))
            else:
                found.append(ForwardSession(
                    session_date=day.isoformat(), state=row["status"], reason=row["reason"],
                    pnl_usd=_money(row["pnl_usd"]), fees_usd=_money(row["fees_usd"]),
                    trades=None if row["trades"] is None else int(row["trades"]),
                    end_equity_usd=_money(row["end_equity_usd"])))
        return found

    def _judgment(self, judgment_id: str) -> Any:
        try:
            judgment = self._judgment_of(judgment_id)
        except JudgmentRefused as refused:                     # a tampered row is named, never read as missing
            raise ForwardEvidenceRefused(refused.code, refused.detail) from None
        if judgment is None:
            raise ForwardEvidenceRefused("JUDGMENT_MISSING", f"the trader holds no judgment {judgment_id}")
        return judgment

    def _sealed_rows(self, judgment_id: str) -> dict[dt.date, dict]:
        """Ruling 9: every row must equal its scoreboard seal."""
        found = {}
        for row in self._scoreboard.fetch("shadow_results", {"judgment_id": judgment_id}):
            seal = self._db.execute("SELECT row_digest FROM scoreboard_seals WHERE table_name = 'shadow_results' "
                                    "AND row_key = ?", [row["record_id"]], fetch="one")
            if seal is None or not hmac.compare_digest(seal[0], row_digest(row)):
                raise ForwardEvidenceRefused("FORWARD_EVIDENCE_TAMPERED",
                                             f"shadow row {row['record_id']} does not match its seal")
            found[row["session_date"]] = row
        return found

    def _trips(self, version_digest: str) -> list[PaperTrip]:
        rows = self._db.execute(
            "SELECT DISTINCT rt.round_trip_id, rt.conid, rt.status, rt.opened_session, rt.closed_session, "
            "rt.net_pnl_usd, rt.fees_complete FROM round_trips rt JOIN ai_paper_decisions d "
            "ON d.decision_id = rt.decision_id WHERE d.action = 'ENTER' AND d.deployment_version = ? "
            "ORDER BY rt.opened_session, rt.round_trip_id", [version_digest], fetch="all")
        return [PaperTrip(round_trip_id=r[0], conid=int(r[1]), status=r[2], opened_session=r[3].isoformat(),
                          closed_session=None if r[4] is None else r[4].isoformat(), net_pnl_usd=_money(r[5]),
                          fees_complete=bool(r[6])) for r in rows]

    @staticmethod
    def _binding(base: AiDeployment) -> VersionBinding:
        return VersionBinding(
            strategy_key=strategy_key(base.strategy_path, base.class_name), strategy_path=base.strategy_path,
            class_name=base.class_name, strategy_file_hash=base.strategy_digest, params=dict(base.params),
            conids=sorted(int(c) for c in base.conids), bar_size=base.bar_size,
            order_notional=float(base.evidence_order_notional), bundle_digest=base.evidence_ref)


def _session(day: dt.date, state: str, reason: Optional[str]) -> ForwardSession:
    return ForwardSession(session_date=day.isoformat(), state=state, reason=reason, pnl_usd=None, fees_usd=None,
                          trades=None, end_equity_usd=None)
```

- [ ] **Step 6:** No run and no commit yet: Task 3 adds `TraderRenewalChecks`, which the test world imports. Go on to Task 3.

---

### Task 3: The real `RenewalChecks` at the trader

**Files:**
- Modify: `trader/automation/deployment_renewal.py` (second half)
- Test: `tests/automation/test_deployment_renewal.py`

**Interfaces:**
- Consumes: Task 2; Plan 1's `RenewalStatus`, `EvaluationCase`, `split_strategy_key`; Plan 2's `same_values`, `bar_size_key`, `normalize_strategy_path`, `registration_command_id`, `AiDeploymentRegistrar.register`.
- Produces: `TraderRenewalChecks(*, forward, gate)` with `status(case, *, now) -> RenewalStatus`; `case_differences(case, facts, sessions) -> list[str]`.

- [ ] **Step 1: Write the failing tests** (`tests/automation/test_deployment_renewal.py`)

```python
"""SP2c Plan 5 Task 3: a RENEWAL judgment at the trader (spec 5.2 item 2, 6.2) and its registration (5.2 item 5)."""
from __future__ import annotations

import datetime as dt

import pytest

from tests.automation.backtest_judge_fixtures import KEY, insert_reject
from tests.automation.renewal_world import AFTER_EXPIRY, RECORD, SESSIONS, UTC, FakeBundles, renewal_world
from trader.automation.ai_deployment_registration import registration_command_id
from trader.automation.ai_deployments import AiDeployment
from trader.automation.ai_judgment_port import cooldown_reader_for
from trader.research.evaluation_case import NO_DEPLOY_MENU


def expired_line(tmp_path, *, rows=SESSIONS, **options):
    rw = renewal_world(tmp_path, **options)
    v1 = rw.deploy_initial()
    for session in rows:
        assert rw.shadow_row(session) == "INSERTED"
    rw.clock.now = AFTER_EXPIRY
    return rw, v1


def test_a_renewal_deploy_needs_an_expired_version_with_complete_forward_rows(tmp_path):  # review focus 5
    rw, v1 = expired_line(tmp_path)
    rw.clock.now = dt.datetime(2026, 10, 13, 22, 0, tzinfo=UTC)                 # evening of the last session
    early = rw.renew(rw.renewal_case(v1), v1)
    assert (early["status"], early["code"]) == ("REFUSED", "RENEWAL_NOT_DUE")
    assert rw.judgments.renewals_of(v1) == ()
    rw.clock.now = AFTER_EXPIRY
    assert rw.renew(rw.renewal_case(v1), v1)["status"] == "RECORDED"
    (renewal,) = rw.judgments.renewals_of(v1)
    assert (renewal.kind, renewal.verdict, renewal.binding["prior_deployment_version"]) == ("RENEWAL", "DEPLOY", v1)
    assert rw.activity.status(v1) == "EXPIRED"                                  # until a renewal is registered


def test_a_missing_or_incomplete_forward_session_blocks_deploy_not_shadow(tmp_path):   # review focus 4
    rw, v1 = expired_line(tmp_path, rows=SESSIONS[:1])
    rw.shadow_row(SESSIONS[1], status="INCOMPLETE")                             # SESSIONS[2] has no row at all
    case = rw.renewal_case(v1)                  # the case claims FORWARD_COMPLETE; the trader reads its own rows
    refused = rw.renew(case, v1)
    assert (refused["status"], refused["code"]) == ("REFUSED", "FORWARD_INCOMPLETE")
    assert rw.renew(case, v1, "SHADOW")["status"] == "RECORDED"
    assert rw.activity.status(v1) == "JUDGMENT_ENDED"


@pytest.mark.parametrize("expires_at", [dt.datetime(2026, 10, 14, 12, tzinfo=UTC),       # already expired
                                        dt.datetime(2026, 10, 15, 12, tzinfo=UTC)])      # no session left
def test_a_renewal_after_the_bundle_expired_cannot_deploy(tmp_path, expires_at):           # review focus 3
    rw, v1 = expired_line(tmp_path, bundles=FakeBundles(expires_at))
    case = rw.renewal_case(v1)
    assert rw.renew(case, v1)["code"] == "BUNDLE_EXPIRED"
    assert rw.renew(case, v1, "SHADOW")["status"] == "RECORDED"                 # Jev may still end the line


def test_a_cooling_key_cannot_renew_with_deploy(tmp_path):
    rw, v1 = expired_line(tmp_path)
    insert_reject(rw.base.db, KEY, dt.date(2026, 10, 30))
    assert rw.renew(rw.renewal_case(v1), v1)["code"] == "FAMILY_COOLING_DOWN"


@pytest.mark.parametrize("changes,detail", [
    ({"selected_params": {"RANGE_MINUTES": 30}, "cohort": [{"RANGE_MINUTES": 30}]}, "params differ"),
    ({"bar_size": "1 min"}, "bar size differs"),
    ({"strategy_file_hash": "sha256:" + "c" * 64}, "file hash differs"),
    ({"artifact_id": "art-9"}, "artifact_id differs"),
    ({"forward_sessions": 20}, "forward_sessions"),
])
def test_a_case_bound_to_other_facts_is_refused_whole(tmp_path, changes, detail):
    rw, v1 = expired_line(tmp_path)
    reply = rw.renew(rw.renewal_case(v1, **changes), v1, "SHADOW")
    assert reply["code"] == "RENEWAL_CASE_MISMATCH" and detail in reply["detail"]
    assert rw.judgments.renewals_of(v1) == ()


def test_a_withdrawn_or_unknown_prior_is_refused(tmp_path):
    rw, v1 = expired_line(tmp_path)
    unknown = "sha256:" + "f" * 64
    assert rw.renew(rw.renewal_case(unknown), unknown, "SHADOW")["code"] == "RENEWAL_PRIOR_INVALID"
    rw.versions.withdraw(v1, reason="operator", principal="cli", command_id="w1")
    assert rw.renew(rw.renewal_case(v1), v1, "SHADOW")["code"] == "RENEWAL_PRIOR_INVALID"


def test_one_renewal_judgment_per_version(tmp_path):                                   # review focus 2
    rw, v1 = expired_line(tmp_path)
    first = rw.renewal_case(v1)
    assert rw.renew(first, v1)["status"] == "RECORDED"
    assert rw.renew(first, v1)["status"] == "EXISTING"
    other = rw.renewal_case(v1, created_at="2026-10-14T22:01:00+00:00")
    assert rw.renew(other, v1, "REJECT", judgment_id="jdg-renewal-2")["code"] == "RENEWAL_ALREADY_JUDGED"
    assert len(rw.judgments.renewals_of(v1)) == 1


def test_a_renewal_reject_ends_the_line_and_cools_the_key_down(tmp_path):              # review focus 5
    rw, v1 = expired_line(tmp_path)
    reply = rw.renew(rw.renewal_case(v1), v1, "REJECT")
    assert reply["status"] == "RECORDED" and reply["cooldown_until_session"] is not None
    assert rw.activity.status(v1) == "JUDGMENT_ENDED"
    assert cooldown_reader_for(rw.base.db).cooling_down(KEY, rw.clock.now) is True
    late = rw.renew(rw.renewal_case(v1, created_at="2026-10-14T22:05:00+00:00"), v1, judgment_id="jdg-renewal-2")
    assert late["code"] == "RENEWAL_LINE_ENDED"


def test_an_incomplete_case_offers_no_deploy(tmp_path):
    rw, v1 = expired_line(tmp_path)
    case = rw.renewal_case(v1, stage="FORWARD_INCOMPLETE", incomplete_sessions=1)
    assert rw.renew(case, v1)["code"] == "JUDGMENT_MENU_MISMATCH"                 # Plan 1: menu is SHADOW/REJECT
    assert rw.renew(case, v1, "REJECT", menu=NO_DEPLOY_MENU)["status"] == "RECORDED"


def test_a_recorded_renewal_deploy_registers_a_fresh_version_on_the_same_base(tmp_path):   # review focus 1, 2
    rw, v1 = expired_line(tmp_path)
    assert rw.renew(rw.renewal_case(v1), v1)["status"] == "RECORDED"
    registrar = rw.registrar()
    body = {"judgment_id": "jdg-renewal-1", "bundle_digest": RECORD["evidence_ref"],
            "deployment": AiDeployment.from_json(RECORD).to_json()}
    command_id = registration_command_id(body, AFTER_EXPIRY.date())
    first = registrar.register(body, principal="ai_research", command_id=command_id)
    again = registrar.register(body, principal="ai_research", command_id=command_id)
    assert first["version_digest"] != v1 and first["digest"] == rw.versions.get(v1).base_digest
    assert (first["kind"], first["first_session"], first["created"]) == ("RENEWAL", "2026-10-15", True)
    assert (again["version_digest"], again["created"]) == (first["version_digest"], False)
    assert rw.activity.status(v1) == "SUPERSEDED"                               # never active, never renewed again
    assert rw.activity.status(first["version_digest"]) == "NOT_STARTED"
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/automation/test_deployment_renewal.py tests/automation/test_version_forward_evidence.py -q`
Expected: FAIL with `ImportError: cannot import name 'TraderRenewalChecks' from 'trader.automation.deployment_renewal'`.

- [ ] **Step 3: Add the second half** (append to `trader/automation/deployment_renewal.py`; add the imports at the top: `from trader.automation.backtest_judgments import RenewalStatus`, `from trader.automation.strategy_binding import bar_size_key, same_values`, `from trader.research.evaluation_case import EvaluationCase`, `from trader.research.strategy_key import split_strategy_key`, `from trader.research.strategy_paths import normalize_strategy_path`)

```python
def case_differences(case: EvaluationCase, facts: VersionFacts, sessions: list[ForwardSession]) -> list[str]:
    """Ruling 6: the renewal case must name exactly the renewed version's binding and window."""
    base, line = facts.base, facts.line
    path, class_name = split_strategy_key(case.strategy_key)
    problems = []
    if normalize_strategy_path(path) != normalize_strategy_path(base.strategy_path) or class_name != base.class_name:
        problems.append(f"strategy key differs: {case.strategy_key}")
    if case.strategy_file_hash != base.strategy_digest:
        problems.append("file hash differs")
    if not same_values(dict(case.selected_params or {}), dict(base.params)):
        problems.append("params differ")
    if list(case.conids) != sorted(int(c) for c in base.conids):
        problems.append("conids differ")
    if bar_size_key(case.bar_size) != bar_size_key(base.bar_size):
        problems.append("bar size differs")
    for name in ("artifact_id", "family_id"):
        value = getattr(case, name)
        if value is not None and value != getattr(line, name):
            problems.append(f"{name} differs")
    if case.renewal.forward_sessions != len(sessions):
        problems.append(f"forward_sessions {case.renewal.forward_sessions} is not the window's {len(sessions)}")
    return problems


class TraderRenewalChecks:
    """Plan 1's RenewalChecks port (spec 5.2 item 2): called before the judgment's write transaction."""

    def __init__(self, *, forward: VersionForwardEvidence, gate: RenewalGate):
        self._forward, self._gate = forward, gate

    def status(self, case: EvaluationCase, *, now: dt.datetime) -> RenewalStatus:
        prior = case.renewal.prior_deployment_version
        try:
            facts = self._forward.facts(prior)
            sessions = self._forward.sessions(facts)
        except ForwardEvidenceRefused as refused:
            code = "RENEWAL_PRIOR_INVALID" if refused.code == "DEPLOYMENT_VERSION_UNKNOWN" else refused.code
            return RenewalStatus(refusal_code=code, detail=refused.detail)
        refusal = self._gate.line_refusal(prior, facts.status)
        if refusal is not None:
            return RenewalStatus(refusal_code=refusal[0], detail=refusal[1])
        differences = case_differences(case, facts, sessions)
        if differences:
            return RenewalStatus(refusal_code="RENEWAL_CASE_MISMATCH", detail="; ".join(differences))
        block = self._gate.deploy_block(facts.base, facts.line, now)
        if block is not None:
            return RenewalStatus(deploy_block_code=block[0], detail=block[1])
        open_sessions = [s.session_date for s in sessions if s.state != "COMPLETE"]
        if open_sessions:
            return RenewalStatus(deploy_block_code="FORWARD_INCOMPLETE",
                                 detail=f"forward sessions not COMPLETE: {open_sessions}")
        return RenewalStatus()
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/automation/test_deployment_renewal.py tests/automation/test_version_forward_evidence.py tests/automation/test_ai_judgment_port.py -q`
Expected: PASS.

- [ ] **Step 5: Commit** (Tasks 2 and 3 together)

```bash
git add trader/research/forward_evidence_view.py trader/automation/deployment_renewal.py \
  trader/automation/ai_judgment_port.py tests/automation/renewal_world.py \
  tests/automation/test_version_forward_evidence.py tests/automation/test_deployment_renewal.py
git commit -m "$(cat <<'EOF'
feat: judge deployment renewals on the trader's own forward evidence

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 4: Wire the renewal ports into the trader

**Files:**
- Modify: `trader/automation/ai_judgment_port.py` (one keyword), `trader/trading/command_stack.py` (`_build_ai_paper_parts`, `_AiPaperParts`, `_build_ai_paper_services`)
- Test: `tests/automation/test_renewal_wiring.py`

**Interfaces:**
- Consumes: Tasks 1–3; Plan 2's `_build_ai_paper_parts` block; Plan 1's `AiPaperServices.forward_evidence`; `served_stack`, `install_seeded_judgments`, `seed_judged_deployment`.
- Produces: `judgment_reader_for(judgments, *, cases_dir, verify_dir, renewals_of=None)`; `_AiPaperParts.forward_evidence`; the trader serves the real `get_deployment_forward_evidence`, real `RenewalChecks` and real `renewals_of`.

- [ ] **Step 1: Write the failing tests** (`tests/automation/test_renewal_wiring.py`)

```python
"""SP2c Plan 5 Task 4: the trader serves the real renewal ports (no default refuses any more)."""
from __future__ import annotations

import pytest

from tests.automation.judged_deployment import install_seeded_judgments, seed_judged_deployment
from tests.sp1_acceptance.conftest import loop_thread  # noqa: F401
from tests.sp1_fixtures import served_stack
from trader.acceptance.scenario import AcceptanceSettings, deployment_record
from trader.automation.deployment_renewal import TraderRenewalChecks, VersionForwardEvidence
from trader.data.schema_migrations import SchemaMigrator
from trader.messaging.typed_rpc import TypedRpcRemoteError

ZERO = "sha256:" + "0" * 64


@pytest.fixture
def served(tmp_path, loop_thread, monkeypatch):
    stack = served_stack(tmp_path, loop_thread, monkeypatch)
    yield stack
    stack.close()


@pytest.fixture
def seeded_served(tmp_path, loop_thread, monkeypatch):
    seeded = install_seeded_judgments(monkeypatch)
    stack = served_stack(tmp_path, loop_thread, monkeypatch)
    stack.seeded = seeded
    yield stack
    stack.close()


def test_the_trader_wires_the_real_renewal_ports(served):
    ai_paper = served.composed.stack.ai_paper
    assert isinstance(ai_paper.forward_evidence, VersionForwardEvidence)
    assert isinstance(ai_paper.judgments._renewals, TraderRenewalChecks)
    assert 125 in SchemaMigrator(served.trader.journal_db).applied_versions()


def test_forward_evidence_is_served_to_research_only(served):
    reply = served.call("research", "get_deployment_forward_evidence", {"deployment_version": ZERO})
    assert (reply["status"], reply["code"], reply["evidence"]) == ("REFUSED", "DEPLOYMENT_VERSION_UNKNOWN", None)
    for principal in ("ai_research", "cli"):
        with pytest.raises(TypedRpcRemoteError) as exc:
            served.call(principal, "get_deployment_forward_evidence", {"deployment_version": ZERO})
        assert exc.value.code == "PERMISSION_DENIED"


def test_the_real_source_reads_the_judgment_store(seeded_served):
    """A seeded version has no recorded judgment: the real source names that (Plan 1's default never looked)."""
    record = deployment_record(AcceptanceSettings(run_id="r", account_id="DU1", strategy_bytes=b"x"))
    _, version = seed_judged_deployment(seeded_served.composed.stack.ai_paper, seeded_served.seeded, record,
                                        today=seeded_served.now().date())
    reply = seeded_served.call("research", "get_deployment_forward_evidence", {"deployment_version": version})
    assert (reply["status"], reply["code"]) == ("REFUSED", "JUDGMENT_MISSING")
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/automation/test_renewal_wiring.py -q`
Expected: FAIL (`assert isinstance(None or NoDeploymentVersions(...), VersionForwardEvidence)`; the seeded read answers `DEPLOYMENT_VERSION_UNKNOWN`).

- [ ] **Step 3: Let the judgment reader take `renewals_of`** (`trader/automation/ai_judgment_port.py`; already applied in Task 2)

```python
def judgment_reader_for(judgments: Any, *, cases_dir: Path, verify_dir: Path,
                        renewals_of: Optional[Callable[[str], Iterable[Any]]] = None) -> JudgmentReader:
    from trader.research.evaluation_case import load_case_verify_keys, load_verified_case

    def read_case(digest: str) -> Any:
        # Keys are loaded per read: nothing touches the disk at trader start, and a rotated key is seen.
        return load_verified_case(cases_dir, digest, load_case_verify_keys(verify_dir))
    return Plan1Judgments(judgments, read_case=read_case, renewals_of=renewals_of or _no_renewals)
```

and change `_no_renewals`'s docstring to `"""No renewal reader given (a stack or test without renewals): no judgment names a version."""`. The test seam `install_seeded_judgments` replaces the function with `lambda judgments, **paths: seeded`, which absorbs the new keyword.

- [ ] **Step 4: Wire the trader** (`trader/trading/command_stack.py`)

`_AiPaperParts` gains `forward_evidence: Any = None`. In `_build_ai_paper_parts`, replace Plan 2's block from `deployments = AiDeploymentStore(...)` to `trader.ai_deployment_versions, trader.ai_deployment_activity = ...` with:

```python
    from trader.automation.deployment_renewal import RenewalGate, TraderRenewalChecks, VersionForwardEvidence
    from trader.scoreboard.store import ScoreboardStore

    deployments = AiDeploymentStore(trader.journal_db, now=now)
    artifacts_root = Path(getattr(trader, "research_artifacts_root", "") or DEFAULT_ARTIFACTS_ROOT).expanduser()
    verify_dir = Path(getattr(trader, "research_verify_dir", "") or DEFAULT_VERIFY_DIR).expanduser()
    cases_dir = artifacts_root / "cases"                          # Plan 1's default_cases_dir() layout
    judge = config.backtest_judge
    calendar = XNYSCalendarPolicy()
    versions = AiDeploymentVersionStore(trader.journal_db, now=now)
    cooldowns = cooldown_reader_for(trader.journal_db)
    bundles = ResearchBundleCheck(artifacts_root=artifacts_root, verify_dir=verify_dir)
    # SP2c Plan 5 ruling 14: judgments, renewal checks and activity need each other. The lambdas are called
    # only after both `backtest_judgments` and `activity` below exist (never while they are built).
    gate = RenewalGate(renewals_of=lambda digest: backtest_judgments.renewals_of(digest), bundles=bundles,
                       cooldowns=cooldowns, calendar=calendar, expiry_sessions=judge.deploy_expiry_sessions)
    forward_evidence = VersionForwardEvidence(
        db=trader.journal_db, scoreboard=ScoreboardStore(trader.journal_db, now=now), versions=versions,
        deployments=deployments, status_of=lambda digest: activity.status(digest),
        judgment_of=lambda judgment_id: backtest_judgments.get(judgment_id), gate=gate, calendar=calendar,
        config=judge, now=now)
    backtest_judgments = BacktestJudgments(trader.journal_db, config=judge, calendar=calendar, cases_dir=cases_dir,
                                           verify_dir=verify_dir, now=now,
                                           renewals=TraderRenewalChecks(forward=forward_evidence, gate=gate))
    judgments = judgment_reader_for(backtest_judgments, cases_dir=cases_dir, verify_dir=verify_dir,
                                    renewals_of=backtest_judgments.renewals_of)
    activity = DeploymentActivity(versions=versions, deployments=deployments, judgments=judgments,
                                  cooldowns=cooldowns, max_active=judge.max_active_deploys, now=now)
    registrar = AiDeploymentRegistrar(
        db=trader.journal_db, deployments=deployments, versions=versions, activity=activity, judgments=judgments,
        cooldowns=cooldowns, bundles=bundles, calendar=calendar, expiry_sessions=judge.deploy_expiry_sessions,
        now=now)
    trader.ai_deployment_versions, trader.ai_deployment_activity = versions, activity   # Plans 1 and 3 read these
```

Pass `forward_evidence=forward_evidence` into `_AiPaperParts(...)`. In `_build_ai_paper_services`, replace `forward_evidence=NoDeploymentVersions()` with `forward_evidence=parts.forward_evidence` and drop the `NoDeploymentVersions` import there (Plan 1's tests still import it from `trader.automation.forward_evidence`). Migration 125 needs no wiring: `apply_backtest_judge_migrations` (Task 1) applies it.

- [ ] **Step 5: Run the tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/automation/test_renewal_wiring.py tests/automation/test_ai_deployment_rpc.py tests/automation/test_backtest_judge_surface.py tests/test_ai_paper_rpc.py tests/test_command_stack.py -q`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add trader/automation/ai_judgment_port.py trader/trading/command_stack.py tests/automation/test_renewal_wiring.py
git commit -m "$(cat <<'EOF'
feat: serve real forward evidence and renewal checks from the trader

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 5: The renewal case builder and its summary

**Files:**
- Create: `trader/research/renewal_case.py`, `tests/research/renewal_fixtures.py`
- Modify: `trader/research/case_builder.py` (`evaluation_summary`)
- Test: `tests/research/test_renewal_case.py`

**Interfaces:**
- Consumes: `ForwardEvidenceView` (Task 2); Plan 1's `EvaluationCase`, `CASE_DOMAIN`, `offered_menu`, `renewal_forward_complete`, `write_evaluation_case`, `load_verified_case`, `FULL_MENU`, `NO_DEPLOY_MENU`; `canonical.sha256_digest`; Plan 4's `CaseSummary` and `parse_reply` (cross-check only).
- Produces: `RENEWAL_REQUEST_DOMAIN`, `renewal_request_id`, `forward_summary`, `pending_sessions`, `build_renewal_case`; `evaluation_summary` of a RENEWAL case.

- [ ] **Step 1: Write the fixtures** (`tests/research/renewal_fixtures.py`)

```python
"""A trader's forward evidence for one version of the fixture TimeOfDay line (Plan 5 Tasks 5 and 6)."""
from __future__ import annotations

import hashlib

from tests.research.evaluation_fixtures import CONIDS, TIME_OF_DAY_STRATEGY

V1 = "sha256:" + "1" * 64
BASE = "sha256:" + "e" * 64
BUNDLE = "sha256:" + "b" * 64
KEY = "strategies/time_of_day.py:TimeOfDay"
PARAMS = {"ENTRY_MINUTE": 600, "EXIT_MINUTE": 660}
SESSIONS = ("2024-04-01", "2024-04-02", "2024-04-03")
FILE_HASH = "sha256:" + hashlib.sha256(TIME_OF_DAY_STRATEGY.encode()).hexdigest()


def session(day: str, state: str = "COMPLETE") -> dict:
    complete = state == "COMPLETE"
    return {"session_date": day, "state": state,
            "reason": None if complete else ("BARS_MISSING: fixture" if state == "INCOMPLETE" else None),
            "pnl_usd": 5.0 if complete else None, "fees_usd": 1.0 if complete else None,
            "trades": 2 if complete else None, "end_equity_usd": 100_005.0 if complete else None}


def forward_view(*, states=("COMPLETE",) * 3, renewable=(True, None), trips=(), file_hash=FILE_HASH) -> dict:
    """Task 2's ForwardEvidenceView on the wire."""
    ok, code = renewable
    return {"version_digest": V1, "base_digest": BASE, "judgment_id": "jdg-00000001", "kind": "INITIAL",
            "prior_version_digest": None, "status": "EXPIRED", "first_session": SESSIONS[0],
            "expiry_session": SESSIONS[-1],
            "binding": {"strategy_key": KEY, "strategy_path": "strategies/time_of_day.py", "class_name": "TimeOfDay",
                        "strategy_file_hash": file_hash, "params": dict(PARAMS), "conids": list(CONIDS),
                        "bar_size": "15 mins", "order_notional": 1900.0, "bundle_digest": BUNDLE},
            "line": {"initial_judgment_id": "jdg-00000001", "family_id": "f" * 64, "selected_trial_id": "t0",
                     "artifact_id": "a" * 64, "eligibility_decision_digest": "d" * 64},
            "renewable": {"ok": ok, "code": code, "detail": "" if ok else code.lower()},
            "sessions": [session(day, state) for day, state in zip(SESSIONS, states)],
            "trips": list(trips), "as_of": "2024-04-04T21:00:00+00:00"}


def trip(round_trip_id="rt-1", net_pnl=7.5, status="CLOSED") -> dict:
    return {"round_trip_id": round_trip_id, "conid": CONIDS[0], "status": status, "opened_session": SESSIONS[0],
            "closed_session": SESSIONS[0] if status == "CLOSED" else None, "net_pnl_usd": net_pnl,
            "fees_complete": True}
```

- [ ] **Step 2: Write the failing tests** (`tests/research/test_renewal_case.py`)

```python
"""SP2c Plan 5 Task 5: a RENEWAL case from forward evidence: no holdout, no trial, cohort = the deployed point."""
from __future__ import annotations

import datetime as dt

import pytest

from tests.research.renewal_fixtures import PARAMS, SESSIONS, V1, forward_view, trip
from trader.ai.research_wire import CaseSummary, parse_reply
from trader.automation.artifact_verifier import ArtifactVerifier, ArtifactVerifierError
from trader.automation.paper_materials import PaperMaterialsError, require_qualified_research_evidence
from trader.research.case_builder import evaluation_summary
from trader.research.evaluation_case import (
    FULL_MENU, NO_DEPLOY_MENU, case_path, load_verified_case, offered_menu, write_evaluation_case,
)
from trader.research.forward_evidence_view import ForwardEvidenceView
from trader.research.renewal_case import build_renewal_case, pending_sessions, renewal_request_id
from trader.research.signing import AttestationSigner

NOW = dt.datetime(2024, 4, 4, 21, 0, tzinfo=dt.timezone.utc)                # the evening after the last session


def build(**view_changes):
    view = ForwardEvidenceView.model_validate(forward_view(**view_changes))
    return build_renewal_case(view, created_at=NOW, warmup_sessions=5)


def test_a_complete_window_is_a_forward_complete_case_that_offers_deploy():
    case = build(trips=(trip(),))
    assert (case.kind, case.stage, case.request_id, case.claim_day) == ("RENEWAL", "FORWARD_COMPLETE", None, None)
    assert case.cohort == [PARAMS] and case.selected_params == PARAMS and offered_menu(case) == FULL_MENU
    assert (case.artifact_id, case.family_id, case.decision_state, case.final_rule_results) == (
        "a" * 64, "f" * 64, None, [])
    assert case.renewal.model_dump() == {"prior_deployment_version": V1, "forward_sessions": 3,
                                         "incomplete_sessions": 0}
    assert (case.evidence["replay_index"], case.evidence["points"], case.evidence["warmup_sessions"]) == (0, [], 5)
    summary = evaluation_summary(case, order_notional=1.0)
    parsed = parse_reply(CaseSummary, "case", summary)                     # Plan 4's strict model accepts it
    assert (parsed.kind, parsed.rules_passed, parsed.renewal_checks_passed, parsed.prior_version_digest) == (
        "RENEWAL", True, True, V1)
    assert (parsed.order_notional, parsed.holdout_passed, parsed.strategy_trials) == (1900.0, None, 0)
    assert parsed.forward["pnl_usd"] == 15.0 and parsed.forward["paper_trips"] == 1
    assert parsed.forward["paper_net_pnl_usd"] == 7.5


@pytest.mark.parametrize("state", ["INCOMPLETE", "MISSING", "NOT_REPLAYED"])
def test_any_session_not_complete_makes_a_forward_incomplete_case(state):                  # review focus 4
    case = build(states=("COMPLETE", state, "COMPLETE"))
    assert (case.stage, case.renewal.incomplete_sessions, offered_menu(case)) == (
        "FORWARD_INCOMPLETE", 1, NO_DEPLOY_MENU)
    summary = evaluation_summary(case, order_notional=1900.0)
    assert (summary["rules_passed"], summary["renewal_checks_passed"], summary["forward"]["pnl_usd"]) == (
        False, False, None)
    assert summary["forward"]["known_pnl_usd"] == 10.0


def test_missing_rows_are_pending_until_the_replay_deadline():
    view = ForwardEvidenceView.model_validate(forward_view(states=("COMPLETE", "COMPLETE", "MISSING")))
    before = dt.datetime(2024, 4, 4, 12, 0, tzinfo=dt.timezone.utc)        # 04-03 close + 17 h is 04-04 13:00 UTC
    assert pending_sessions(view, now=before, incomplete_after_hours=16) == [SESSIONS[2]]
    assert pending_sessions(view, now=NOW, incomplete_after_hours=16) == []


def test_the_renewal_request_id_names_the_version_only():
    assert renewal_request_id(V1) == renewal_request_id(V1) and renewal_request_id(V1).startswith("sha256:")
    assert renewal_request_id("sha256:" + "2" * 64) != renewal_request_id(V1)


def test_a_signed_renewal_case_verifies_and_is_never_bundle_evidence(tmp_path):
    signer = AttestationSigner.generate()
    case = build()
    digest = write_evaluation_case(tmp_path / "cases", case, signer)
    assert load_verified_case(tmp_path / "cases", digest, {signer.public_key_id: signer.public_key}) == case
    path = case_path(tmp_path / "cases", digest)
    with pytest.raises(ArtifactVerifierError):
        ArtifactVerifier([signer.public_key]).verify(path, "paper", case.artifact_id, NOW)
    with pytest.raises(PaperMaterialsError):
        require_qualified_research_evidence(path)
```

- [ ] **Step 3: Run the tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/research/test_renewal_case.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'trader.research.renewal_case'`.

- [ ] **Step 4: Implement** `trader/research/renewal_case.py`

```python
"""The RENEWAL evaluation case (SP2c spec 6.2 "Renewal after expiry"): code-built from the trader's forward
evidence, signed like any case. No holdout is opened and no parameter trial is written."""
from __future__ import annotations

import datetime as dt
from collections import Counter

import exchange_calendars as xcals
import pandas as pd

from trader.research.canonical import sha256_digest
from trader.research.evaluation_case import CASE_DOMAIN, EvaluationCase
from trader.research.forward_evidence_view import ForwardEvidenceView

RENEWAL_REQUEST_DOMAIN = "mmr.research.renewal-request.v1"
ROW_GRACE_HOURS = 1
_CALENDAR = xcals.get_calendar("XNYS")


def renewal_request_id(prior_version_digest: str) -> str:
    """Ruling 3: one renewal request per version; the caller cannot choose the id."""
    return "sha256:" + sha256_digest(RENEWAL_REQUEST_DOMAIN,
                                     {"kind": "RENEWAL", "prior_version_digest": prior_version_digest})


def pending_sessions(view: ForwardEvidenceView, *, now: dt.datetime, incomplete_after_hours: float) -> list[str]:
    """Ruling 5: a MISSING row is still on its way until the replay's INCOMPLETE deadline plus one hour."""
    waiting = []
    for session in view.sessions:
        if session.state != "MISSING":
            continue
        deadline = _CALENDAR.session_close(pd.Timestamp(session.session_date)) + pd.Timedelta(
            hours=incomplete_after_hours + ROW_GRACE_HOURS)
        if pd.Timestamp(now) < deadline:
            waiting.append(session.session_date)
    return waiting


def forward_summary(view: ForwardEvidenceView) -> dict:
    """What Jev reads about the forward window: code-computed sums, never summed across incomplete days."""
    complete = [s for s in view.sessions if s.state == "COMPLETE"]
    other = [s for s in view.sessions if s.state != "COMPLETE"]
    known = float(sum(s.pnl_usd for s in complete)) if complete else None
    closed = [t for t in view.trips if t.status == "CLOSED"]
    priced = [t.net_pnl_usd for t in closed if t.net_pnl_usd is not None]
    return {"first_session": view.first_session, "expiry_session": view.expiry_session,
            "sessions": len(view.sessions), "complete": len(complete), "incomplete": len(other),
            "incomplete_reasons": dict(Counter(s.reason or s.state for s in other)),
            "pnl_usd": None if other else known, "known_pnl_usd": known,
            "fees_usd": float(sum(s.fees_usd for s in complete)) if complete else None,
            "trades": sum(s.trades for s in complete),
            "worst_session_pnl_usd": min(s.pnl_usd for s in complete) if complete else None,
            "end_equity_usd": complete[-1].end_equity_usd if complete else None,
            "paper_trips": len(view.trips), "paper_trips_closed": len(closed),
            "paper_net_pnl_usd": float(sum(priced)) if priced else None,
            "paper_fees_complete": all(t.fees_complete for t in view.trips)}


def build_renewal_case(view: ForwardEvidenceView, *, created_at: dt.datetime, warmup_sessions: int) -> EvaluationCase:
    """Ruling 12: the deployed point only; the line's artifact facts; FORWARD_COMPLETE only when every
    session is COMPLETE. The evidence keeps the replay keys so the renewal judgment joins the shadow cohort."""
    binding, line = view.binding, view.line
    incomplete = sum(1 for s in view.sessions if s.state != "COMPLETE")
    params = dict(binding.params)
    return EvaluationCase(
        schema_version=CASE_DOMAIN, kind="RENEWAL", request_id=None, claim_day=None,
        strategy_key=binding.strategy_key, strategy_file_hash=binding.strategy_file_hash, cohort=[params],
        conids=list(binding.conids), bar_size=binding.bar_size,
        stage="FORWARD_COMPLETE" if view.sessions and incomplete == 0 else "FORWARD_INCOMPLETE",
        holdout_passed=None, selected_params=params, family_id=line.family_id, selected_trial_id=line.selected_trial_id,
        artifact_id=line.artifact_id, eligibility_decision_digest=line.eligibility_decision_digest,
        decision_state=None, ruleset_digest=None, final_rule_results=[],
        renewal={"prior_deployment_version": view.version_digest, "forward_sessions": len(view.sessions),
                 "incomplete_sessions": incomplete},
        created_at=created_at.isoformat(),
        evidence={"points": [], "strategy_trials": None, "selected_index": None, "replay_index": 0,
                  "holdout": None, "previously_revealed": [], "holdouts_opened_before": None,
                  "warmup_sessions": warmup_sessions, "error": None, "forward": forward_summary(view),
                  "forward_sessions": [s.model_dump(mode="json") for s in view.sessions],
                  "paper_trips": [t.model_dump(mode="json") for t in view.trips],
                  "order_notional": binding.order_notional, "bundle_digest": binding.bundle_digest,
                  "initial_judgment_id": line.initial_judgment_id})
```

- [ ] **Step 5: Fill the renewal keys of the summary** (`trader/research/case_builder.py`)

Import `renewal_forward_complete` next to `offered_menu`. In `evaluation_summary`, replace the four hardcoded entries:

```python
        "eligibility": case.decision_state,
        "renewal_checks_passed": renewal_forward_complete(case) if case.kind == "RENEWAL" else None,
        "prior_version_digest": None if case.renewal is None else case.renewal.prior_deployment_version,
        "order_notional": float(evidence["order_notional"] if case.kind == "RENEWAL" else order_notional),
```

and the last entry `"forward": None` with `"forward": evidence.get("forward"),`. An INITIAL case's evidence has no `forward` key, so its summary is unchanged.

- [ ] **Step 6: Run the tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/research/test_renewal_case.py tests/research/test_case_builder.py -q`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add trader/research/renewal_case.py trader/research/case_builder.py tests/research/renewal_fixtures.py \
  tests/research/test_renewal_case.py
git commit -m "$(cat <<'EOF'
feat: build signed renewal cases from forward evidence

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 6: The RENEWAL kind of `submit_evaluation`

**Files:**
- Create: `trader/research/renewal_service.py`
- Modify: `trader/research/trader_port.py` (`forward_evidence`), `trader/research/service_store.py` (`record_renewal`), `trader/research/evaluation_service.py` (delegation), `trader/research_service.py` (wiring), `tests/research/service_fakes.py` (`FakeTrader.forward_evidence`)
- Test: `tests/research/test_renewal_service.py`

**Interfaces:**
- Consumes: Task 5; Plan 3's `ResearchStore`, `EvaluationService`, `TraderPort`, `TraderUnavailable`, `JudgmentAttest`, `ShadowReplay`, `ResearchServiceConfig.shadow_incomplete_after_hours`; Plan 1's `BacktestJudgeConfig.allows`.
- Produces: `RenewalRequests.submit(prior_version_digest) -> dict`; `EvaluationService(..., renewals=None)`; `TraderPort.forward_evidence(version_digest) -> dict`; `ResearchStore.record_renewal(request_id, body, *, strategy_key, file_hash, case_digest, stage, summary, now)`.

- [ ] **Step 1: Extend the fake trader** (`tests/research/service_fakes.py`, in `FakeTrader.__init__` add `self.forward, self.forward_reads = {}, []`, and add the method)

```python
    def forward_evidence(self, version_digest):
        """Plan 1's get_deployment_forward_evidence reply; Plan 5 owns the evidence shape."""
        self.forward_reads.append(version_digest)
        evidence = self.forward.get(version_digest)
        if evidence is None:
            return {"status": "REFUSED", "code": "DEPLOYMENT_VERSION_UNKNOWN", "detail": version_digest,
                    "evidence": None}
        return {"status": "FOUND", "code": None, "detail": None, "evidence": evidence}
```

- [ ] **Step 2: Write the failing tests** (`tests/research/test_renewal_service.py`)

```python
"""SP2c Plan 5 Task 6: submit_evaluation kind RENEWAL: no claim, no queue, no trial; refused before any case."""
from __future__ import annotations

import datetime as dt
from types import SimpleNamespace

import pytest

from tests.research.evaluation_fixtures import TIME_OF_DAY_STRATEGY
from tests.research.renewal_fixtures import KEY, V1, forward_view
from tests.research.service_fakes import AI, CLI, FakeTrader, judgment_view
from trader.automation.backtest_judge_config import BacktestJudgeConfig
from trader.data.duckdb_store import DuckDBConnection
from trader.data.schema_migrations import SchemaMigrator
from trader.research.evaluation_case import load_verified_case
from trader.research.evaluation_service import EvaluationService
from trader.research.judgment_attest import JudgmentAttest
from trader.research.renewal_case import renewal_request_id
from trader.research.renewal_service import RenewalRequests
from trader.research.schema import apply_research_migrations
from trader.research.service_config import ResearchServiceConfig
from trader.research.service_store import ResearchStore
from trader.research.shadow_replay import ShadowReplay
from trader.research.signing import AttestationSigner
from trader.research.trader_port import TraderUnavailable

NOW = dt.datetime(2024, 4, 4, 21, 0, tzinfo=dt.timezone.utc)
RENEWAL = {"kind": "RENEWAL", "prior_version_digest": V1}


@pytest.fixture
def world(tmp_path):
    (tmp_path / "strategies").mkdir()
    (tmp_path / "strategies" / "time_of_day.py").write_text(TIME_OF_DAY_STRATEGY)
    db = DuckDBConnection.get_instance(str(tmp_path / "research.duckdb"))
    apply_research_migrations(SchemaMigrator(db))
    store, trader, signer = ResearchStore(db), FakeTrader(), AttestationSigner.generate()
    clock = {"now": NOW}
    trader.forward[V1] = forward_view()

    def service(*, allowlist=(KEY,)):
        renewals = RenewalRequests(store=store, trader=trader, signer=signer, artifacts_root=tmp_path / "artifacts",
                                   repo_root=tmp_path, judge=BacktestJudgeConfig(strategy_allowlist=allowlist),
                                   warmup_sessions=5, incomplete_after_hours=16, now=lambda: clock["now"])
        return EvaluationService(store=store, trader=trader, build_spec=_never, evaluate=_never, signer=signer,
                                 artifacts_root=tmp_path / "artifacts", warmup_sessions=5, order_notional=1900.0,
                                 queue_max=2, now=lambda: clock["now"], renewals=renewals)
    return SimpleNamespace(service=service, trader=trader, store=store, signer=signer, root=tmp_path, clock=clock,
                           db=db)


def _never(*args):
    raise AssertionError("a renewal builds no cohort spec and runs no evaluation")


def test_a_renewal_case_is_built_once_from_the_traders_forward_evidence(world):
    service = world.service()
    reply = service.submit(RENEWAL, AI)
    assert (reply["status"], reply["request_id"], reply["state"]) == ("ACCEPTED", renewal_request_id(V1), "DONE")
    view = service.get(reply["request_id"], CLI)
    assert view["found"] and view["state"] == "DONE" and view["summary"]["kind"] == "RENEWAL"
    case = load_verified_case(world.root / "artifacts" / "cases", view["case_digest"],
                              {world.signer.public_key_id: world.signer.public_key})
    assert (case.stage, case.renewal.prior_deployment_version) == ("FORWARD_COMPLETE", V1)
    assert world.store.case(case_digest=view["case_digest"])["request_id"] == reply["request_id"]
    again = service.submit(RENEWAL, AI)
    assert (again["status"], again["request_id"]) == ("DUPLICATE", reply["request_id"])
    assert world.trader.claims == {} and world.trader.updates == [] and world.trader.forward_reads == [V1]


@pytest.mark.parametrize("change,code,retryable", [
    ({"renewable": (False, "BUNDLE_EXPIRED")}, "BUNDLE_EXPIRED", False),
    ({"renewable": (False, "RENEWAL_NOT_DUE")}, "RENEWAL_NOT_DUE", False),
    ({"renewable": (False, "FAMILY_COOLING_DOWN")}, "FAMILY_COOLING_DOWN", False),
    ({"file_hash": "sha256:" + "c" * 64}, "STRATEGY_SOURCE_CHANGED", False),
    ({"states": ("COMPLETE", "COMPLETE", "MISSING")}, "FORWARD_EVIDENCE_PENDING", True),
])
def test_a_renewal_that_cannot_be_renewed_is_refused_before_any_case(world, change, code, retryable):  # focus 3
    world.trader.forward[V1] = forward_view(**change)
    if code == "FORWARD_EVIDENCE_PENDING":
        world.clock["now"] = dt.datetime(2024, 4, 4, 12, 0, tzinfo=dt.timezone.utc)
    reply = world.service().submit(RENEWAL, AI)
    assert (reply["status"], reply["code"], reply["retryable"]) == ("REFUSED", code, retryable)
    assert world.store.get(renewal_request_id(V1)) is None                     # a later resend asks again
    assert not (world.root / "artifacts" / "cases").exists() or not any((world.root / "artifacts" / "cases").iterdir())


def test_unknown_versions_off_allowlist_keys_and_an_unreachable_trader_are_refused(world):
    other = {"kind": "RENEWAL", "prior_version_digest": "sha256:" + "2" * 64}
    assert world.service().submit(other, AI)["code"] == "DEPLOYMENT_VERSION_UNKNOWN"
    assert world.service(allowlist=()).submit(RENEWAL, AI)["code"] == "STRATEGY_NOT_ALLOWED"

    def down(version):
        raise TraderUnavailable("no route")
    world.trader.forward_evidence = down
    reply = world.service().submit(RENEWAL, AI)
    assert (reply["code"], reply["retryable"]) == ("TRADER_UNAVAILABLE", True)


def test_only_ai_research_submits_a_renewal(world):
    assert world.service().submit(RENEWAL, CLI)["code"] == "PRINCIPAL_FORBIDDEN"


def test_recover_skips_renewal_rows_which_have_no_claim(world, monkeypatch):
    service = world.service()
    service.submit(RENEWAL, AI)

    def readback(request_id):
        assert request_id != renewal_request_id(V1), "a renewal has no claim to read back"
        return None
    monkeypatch.setattr(world.trader, "claim_readback", readback)
    world.service().recover()


def test_attest_refuses_a_renewal_judgment(world):
    world.trader.judgments["jdg-renewal-1"] = judgment_view("jdg-renewal-1", "sha256:" + "9" * 64, "DEPLOY",
                                                            kind="RENEWAL")
    attest = JudgmentAttest(research_db=world.db, store=world.store, trader=world.trader, signer=world.signer,
                            artifacts_root=world.root / "artifacts", repo_root=world.root, is_paper=lambda: True,
                            now=lambda: NOW)
    reply = attest.attest({"judgment_id": "jdg-renewal-1"}, AI)
    assert (reply["status"], reply["code"]) == ("REFUSED", "JUDGMENT_NOT_DEPLOY")


def test_a_judged_renewal_case_joins_the_shadow_cohort(world):
    reply = world.service().submit(RENEWAL, AI)
    digest = world.store.get(reply["request_id"])["case_digest"]
    world.trader.judgments["jdg-renewal-1"] = judgment_view("jdg-renewal-1", digest, "DEPLOY", kind="RENEWAL",
                                                            decided_at="2024-04-04T21:30:00+00:00")
    judge = SimpleNamespace(deploy_expiry_sessions=3, family_cooldown_sessions=10, shadow_warmup_sessions=5)
    replay = ShadowReplay(store=world.store, trader=world.trader, signer=world.signer,
                          artifacts_root=world.root / "artifacts", paths=None, registry=None,
                          config=ResearchServiceConfig(), judge=judge, now=lambda: NOW)
    replay.tick()                                                   # no session is due yet: it only joins
    (member,) = world.store.shadow_members()
    assert (member["judgment_id"], member["verdict"], str(member["first_session"])) == (
        "jdg-renewal-1", "DEPLOY", "2024-04-05")
```

- [ ] **Step 3: Run the tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/research/test_renewal_service.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'trader.research.renewal_service'`.

- [ ] **Step 4: Implement** `trader/research/renewal_service.py`

```python
"""submit_evaluation, kind RENEWAL (SP2c spec 6.2): sign a renewal case from the trader's forward evidence.

No claim, no queue, no backtest, no trial, no holdout (rulings 2 and 3). Anything that makes the renewal
impossible is refused before a case exists, so Jev is never asked about it (ruling 8)."""
from __future__ import annotations

import datetime as dt
import hashlib
import threading
from pathlib import Path
from typing import Any, Callable, Optional

from trader.research.case_builder import evaluation_summary
from trader.research.evaluation_case import write_evaluation_case
from trader.research.forward_evidence_view import ForwardEvidenceView
from trader.research.renewal_case import build_renewal_case, pending_sessions, renewal_request_id
from trader.research.trader_port import TraderUnavailable


def _reply(status: str, request_id: Optional[str] = None, state: Optional[str] = None, code: Optional[str] = None,
           detail: Optional[str] = None, retryable: bool = False) -> dict:
    """Plan 3's submit_evaluation reply shape."""
    return {"status": status, "request_id": request_id, "state": state, "code": code, "detail": detail,
            "retryable": retryable}


class RenewalRequests:
    def __init__(self, *, store: Any, trader: Any, signer: Any, artifacts_root: Path, repo_root: Path, judge: Any,
                 warmup_sessions: int, incomplete_after_hours: float, now: Callable[[], dt.datetime]):
        self._store, self._trader, self._signer = store, trader, signer
        self._cases_dir, self._repo_root = Path(artifacts_root) / "cases", Path(repo_root)
        self._judge, self._warmup_sessions = judge, warmup_sessions
        self._incomplete_after_hours, self._now = incomplete_after_hours, now
        self._lock = threading.Lock()

    def submit(self, prior_version_digest: str) -> dict:
        request_id = renewal_request_id(prior_version_digest)
        with self._lock:
            row = self._store.get(request_id)
            if row is not None:
                return _reply("DUPLICATE", request_id, row["state"])
            try:
                reply = self._trader.forward_evidence(prior_version_digest)
            except TraderUnavailable as exc:
                return _reply("REFUSED", request_id, code="TRADER_UNAVAILABLE", detail=str(exc), retryable=True)
            if reply["status"] != "FOUND":
                return _reply("REFUSED", request_id, code=reply["code"], detail=reply["detail"])
            view = ForwardEvidenceView.model_validate(reply["evidence"])     # a bad shape raises: fail loudly
            if view.version_digest != prior_version_digest:
                raise ValueError(f"the trader answered for {view.version_digest}, not {prior_version_digest}")
            refusal = self._refusal(view)
            if refusal is not None:
                code, detail, retryable = refusal
                return _reply("REFUSED", request_id, code=code, detail=detail, retryable=retryable)
            created_at = self._now()
            case = build_renewal_case(view, created_at=created_at, warmup_sessions=self._warmup_sessions)
            digest = write_evaluation_case(self._cases_dir, case, self._signer)
            self._store.record_renewal(
                request_id, {"kind": "RENEWAL", "prior_version_digest": prior_version_digest},
                strategy_key=case.strategy_key, file_hash=case.strategy_file_hash, case_digest=digest,
                stage=case.stage, summary=evaluation_summary(case, order_notional=view.binding.order_notional),
                now=created_at)
            return _reply("ACCEPTED", request_id, "DONE")

    def _refusal(self, view: ForwardEvidenceView) -> Optional[tuple[str, str, bool]]:
        if not view.renewable.ok:
            return view.renewable.code or "RENEWAL_REFUSED", view.renewable.detail, False
        if not self._judge.allows(view.binding.strategy_key):
            return "STRATEGY_NOT_ALLOWED", f"{view.binding.strategy_key} is off strategy_allowlist", False
        if self._file_hash(view.binding.strategy_path) != view.binding.strategy_file_hash:
            return "STRATEGY_SOURCE_CHANGED", "the strategy file on disk is not the deployed bytes", False
        waiting = pending_sessions(view, now=self._now(), incomplete_after_hours=self._incomplete_after_hours)
        if waiting:
            return "FORWARD_EVIDENCE_PENDING", f"shadow rows not final yet for {waiting}", True
        return None

    def _file_hash(self, strategy_path: str) -> Optional[str]:
        try:
            return "sha256:" + hashlib.sha256((self._repo_root / strategy_path).read_bytes()).hexdigest()
        except OSError:
            return None
```

`trader/research/trader_port.py`, add to `TraderPort`:

```python
    def forward_evidence(self, version_digest: str) -> dict:
        """Plan 1's get_deployment_forward_evidence; the evidence is Plan 5's ForwardEvidenceView."""
        return self._call(self._query, "get_deployment_forward_evidence", {"deployment_version": version_digest})
```

`trader/research/service_store.py`, add to `ResearchStore`:

```python
    def record_renewal(self, request_id: str, body: dict, *, strategy_key: str, file_hash: str, case_digest: str,
                       stage: str, summary: dict, now: dt.datetime) -> None:
        """A renewal request is DONE at once (Plan 5 rulings 2-3): the request and its case in one transaction."""
        def tx(conn):
            if conn.execute("SELECT 1 FROM research_requests WHERE request_id = ?", [request_id]).fetchone():
                return
            conn.execute("INSERT INTO research_requests (request_id, body_json, strategy_key, file_hash, state, "
                         "case_digest, summary_json, created_at, updated_at) VALUES (?, ?, ?, ?, 'DONE', ?, ?, ?, ?)",
                         [request_id, json.dumps(body, sort_keys=True), strategy_key, file_hash, case_digest,
                          json.dumps(summary), now, now])
            conn.execute("INSERT INTO research_cases VALUES (?, ?, ?, ?)", [case_digest, request_id, stage, now])
        self._db.transaction(tx)
```

`trader/research/evaluation_service.py`:
- `EvaluationService.__init__` gains a last keyword `renewals: Any = None` (`self._renewals = renewals`).
- In `submit`, replace the `if raw.get("kind") != "INITIAL": ... RENEWAL_NOT_SUPPORTED` lines with:

```python
        if raw.get("kind") == "RENEWAL":
            if self._renewals is None:
                return _submit_reply("REFUSED", code="RENEWAL_NOT_SUPPORTED", detail="this service builds no renewals")
            return self._renewals.submit(raw["prior_version_digest"])
        if raw.get("kind") != "INITIAL":
            return _submit_reply("REFUSED", code="REQUEST_INVALID", detail="kind must be INITIAL or RENEWAL")
```

- `recover` and `run_next` need no change: they report only rows whose `pending_report` is set (Plan 3 ruling 24), and `record_renewal` writes a `DONE` row with `pending_report` null, so a renewal is never reported to the trader and `get_evaluation` shows its case at once (a renewal has no claim, ruling 2).

`trader/research_service.py`, in `build_runtime` after `attest = JudgmentAttest(...)`: build

```python
    renewals = RenewalRequests(store=store, trader=trader, signer=signer, artifacts_root=ARTIFACTS_ROOT,
                               repo_root=root, judge=judge, warmup_sessions=judge.shadow_warmup_sessions,
                               incomplete_after_hours=config.shadow_incomplete_after_hours, now=now)
```

move the `EvaluationService(...)` construction below it and pass `renewals=renewals`. Import `RenewalRequests`.

- [ ] **Step 5: Run the tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/research/test_renewal_service.py tests/research/test_evaluation_service.py tests/research/test_judgment_attest.py tests/research/test_research_surface.py -q`
Expected: PASS (Plan 3's `test_callers_queue_and_renewal` still gets `RENEWAL_NOT_SUPPORTED`: its service has no `renewals`).

- [ ] **Step 6: Commit**

```bash
git add trader/research/renewal_service.py trader/research/trader_port.py trader/research/service_store.py \
  trader/research/evaluation_service.py trader/research_service.py tests/research/service_fakes.py \
  tests/research/test_renewal_service.py
git commit -m "$(cat <<'EOF'
feat: serve renewal requests from the research service

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 7: The controller asks for renewals

**Files:**
- Modify: `trader/ai/backtest_judge.py` (`judgment_id_for`, `judgment_body`), `trader/ai/research_roles.py` (`BACKTEST_SYSTEM`), `trader/ai/research_cycle.py` (slot and pump), `tests/ai/research/cases.py` (renewal replies), `tests/ai/research/test_research_slot_cycle.py` (Plan 4's line test)
- Test: `tests/ai/research/test_research_renewal.py`

**Interfaces:**
- Consumes: Plan 4's `ResearchCycle`, `Rig`, `ScriptedClient`, `registration_body`, `Binding`, `parse_reply`, `canonical_json`; Task 6's RENEWAL replies.
- Produces: `judgment_id_for(case_digest, kind="INITIAL")`; `renewal_candidate_id(prior_version_digest)`; `ResearchCycle._end_finished_lines(slot)`, `_reopen_stranded_renewals_in_tx`, `_request_renewal_in_tx`, `_end_line_in_tx`, `_renewal_registration_in_tx`, `_close_candidate`.

- [ ] **Step 1: Add the renewal replies** (append to `tests/ai/research/cases.py`)

```python
RENEWAL_CASE, RENEWAL_REQUEST, V2 = "sha256:" + "9" * 64, "sha256:" + "8" * 64, "sha256:" + "2" * 64


def renewal_summary(complete=True, prior=V1):
    """Plan 3's evaluation_summary of a RENEWAL case (Plan 5 Task 5)."""
    return {**summary(), "kind": "RENEWAL", "stage": "FORWARD_COMPLETE" if complete else "FORWARD_INCOMPLETE",
            "rules_passed": complete, "holdout_passed": None, "eligibility": None, "renewal_checks_passed": complete,
            "prior_version_digest": prior, "strategy_trials": 0, "prior_holdouts": 0,
            "previously_revealed_sessions": 0, "selected_index": None, "error": None, "metrics": {}, "points": [],
            "rule_results": [], "forward": {"sessions": 3, "complete": 3 if complete else 2,
                                            "incomplete": 0 if complete else 1}}


def renewal_done(**fields):
    return view("DONE", request_id=RENEWAL_REQUEST, case=RENEWAL_CASE, summary_=renewal_summary(**fields))


def version_reply(state, version=V1):
    return {"found": True, "version": {"version_digest": version, "base_digest": BASE, "judgment_id": "jdg-old",
                                       "kind": "INITIAL", "prior_version_digest": None,
                                       "first_session": "2026-09-10", "expiry_session": "2026-10-07",
                                       "state": state}}


def renewed():
    outcome = {**resolved(V2)["outcome"], "kind": "RENEWAL", "first_session": "2026-10-12",
               "expiry_session": "2026-11-06"}
    return {**resolved(V2), "outcome": outcome}
```

- [ ] **Step 2: Write the failing tests** (`tests/ai/research/test_research_renewal.py`)

```python
"""SP2c Plan 5 Task 7: an EXPIRED line asks for a renewal; the verdict renews it or ends it."""
import hashlib
import json

import pytest
import pytest_asyncio

from tests.ai.research.cases import (
    BASE, BUNDLE, KEY, RENEWAL_CASE, RENEWAL_REQUEST, V1, V2, binding, recorded, refused, renewal_done, renewed,
    submitted, version_reply,
)
from tests.ai.research.rig import Rig, ruling
from trader.ai.backtest_judge import judgment_id_for
from trader.ai.ids import canonical_json
from trader.ai.research_cycle import registration_body, renewal_candidate_id
from trader.ai.research_roles import BACKTEST_MARKER, RESEARCH_MARKER
from trader.ai.research_wire import Binding, parse_reply

NO_CANDIDATES = json.dumps({"candidates": []})


@pytest_asyncio.fixture
async def rig(tmp_path):
    built = Rig(tmp_path)
    await built.start()
    return built


def seed_live_line(rig):
    """A registered INITIAL line as Plan 4 leaves it; returns its registration body."""
    body = canonical_json(registration_body(parse_reply(Binding, "binding", binding()), judgment_id="jdg-old",
                                            bundle_digest=BUNDLE))
    rig.store.db.execute(
        "INSERT INTO ai_research_registrations (judgment_id, kind, strategy_key, bundle_digest, body_json, "
        "body_sha256, state, base_digest, version_digest, expiry_session, line_state, next_try_at, created_at, "
        "updated_at) VALUES ('jdg-old', 'INITIAL', ?, ?, ?, ?, 'REGISTERED', ?, ?, '2026-10-07', 'LIVE', now(), "
        "now(), now())", [KEY, BUNDLE, body, hashlib.sha256(body.encode()).hexdigest(), BASE, V1])
    return json.loads(body)


async def renewal_night(rig, *, verdict="DEPLOY", submit=None, version_state="EXPIRED", pumps=6,
                        proposal=NO_CANDIDATES, **summary_fields):
    rig.registry.script("get_ai_deployment_version", version_reply(version_state))
    rig.orchestrator.script(RESEARCH_MARKER, proposal)
    rig.lab.script("submit_evaluation", *(submit or [submitted(request_id=RENEWAL_REQUEST, state="DONE")]))
    rig.lab.script("get_evaluation", renewal_done(**summary_fields))
    rig.jev.script(BACKTEST_MARKER, ruling(verdict))
    rig.registry.script("record_backtest_judgment", recorded)
    rig.registry.script("register_ai_deployment", renewed())
    cycle = rig.cycle()
    await cycle.run_due_slot()
    for _ in range(pumps):
        await cycle.pump()
        rig.clock.advance(31)
    return cycle


def line(rig, version=V1):
    return rig.rows("SELECT line_state, error_code FROM ai_research_registrations WHERE version_digest = ?", [version])


@pytest.mark.asyncio
async def test_an_expired_line_is_renewed_on_the_same_bundle_without_attestation(rig):     # review focus 1
    prior_body = seed_live_line(rig)
    await renewal_night(rig)
    assert rig.lab.sent("submit_evaluation") == [{"kind": "RENEWAL", "prior_version_digest": V1}]
    (record,) = rig.registry.sent("record_backtest_judgment")
    assert (record["kind"], record["renewal_of_version"], record["case_digest"], record["menu"]) == (
        "RENEWAL", V1, RENEWAL_CASE, ["DEPLOY", "SHADOW", "REJECT"])
    assert record["judgment_id"] == judgment_id_for(RENEWAL_CASE, "RENEWAL") != judgment_id_for(RENEWAL_CASE)
    assert rig.lab.sent("attest_from_judgment") == []
    assert rig.registry.sent("register_ai_deployment") == [{**prior_body, "judgment_id": record["judgment_id"]}]
    assert rig.rows("SELECT kind, prior_version_digest, state, version_digest, line_state FROM "
                    "ai_research_registrations WHERE kind = 'RENEWAL'") == [("RENEWAL", V1, "REGISTERED", V2, "LIVE")]
    assert line(rig) == [("ENDED", "RENEWED")]


@pytest.mark.asyncio
@pytest.mark.parametrize("verdict", ["SHADOW", "REJECT"])
async def test_a_non_deploy_renewal_ends_the_line(rig, verdict):                             # review focus 5
    seed_live_line(rig)
    await renewal_night(rig, verdict=verdict)
    assert line(rig) == [("ENDED", f"RENEWAL_{verdict}")]
    assert rig.registry.sent("register_ai_deployment") == []
    assert rig.rows("SELECT source FROM ai_research_cooldowns") == ([("REJECT",)] if verdict == "REJECT" else [])


@pytest.mark.asyncio
async def test_an_incomplete_forward_window_never_offers_deploy(rig):
    seed_live_line(rig)
    await renewal_night(rig, verdict="DEPLOY", complete=False)
    (record,) = rig.registry.sent("record_backtest_judgment")
    assert (record["verdict"], record["menu"]) == ("NO_VERDICT", ["SHADOW", "REJECT"])
    assert line(rig) == [("ENDED", "RENEWAL_NO_VERDICT")]


@pytest.mark.asyncio
@pytest.mark.parametrize("code", ["BUNDLE_EXPIRED", "RENEWAL_LINE_ENDED", "FAMILY_COOLING_DOWN",
                                  "STRATEGY_SOURCE_CHANGED"])
async def test_a_refused_renewal_ends_the_line_without_asking_jev(rig, code):
    seed_live_line(rig)
    await renewal_night(rig, submit=[refused(code, request_id=RENEWAL_REQUEST)])
    assert rig.rows("SELECT state, end_code FROM ai_research_candidates") == [("CLOSED", f"REFUSED_{code}")]
    assert line(rig) == [("ENDED", f"RENEWAL_REFUSED_{code}")]
    assert rig.jev.requests == []


@pytest.mark.asyncio
async def test_a_pending_forward_window_is_asked_again(rig):
    seed_live_line(rig)
    await renewal_night(rig, submit=[refused("FORWARD_EVIDENCE_PENDING", request_id=RENEWAL_REQUEST, retryable=True),
                                     submitted(request_id=RENEWAL_REQUEST, state="DONE")], pumps=8)
    assert len(rig.lab.sent("submit_evaluation")) == 2
    assert line(rig) == [("ENDED", "RENEWED")]


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["WITHDRAWN", "ENDED"])
async def test_a_withdrawn_or_ended_version_ends_its_line_without_a_renewal(rig, state):
    seed_live_line(rig)
    await renewal_night(rig, version_state=state, pumps=0)
    assert line(rig) == [("ENDED", state)] and rig.lab.calls == []
    assert rig.rows("SELECT COUNT(*) FROM ai_research_candidates") == [(0,)]


@pytest.mark.asyncio
async def test_a_renewal_is_requested_once_per_version(rig):
    seed_live_line(rig)
    await renewal_night(rig, pumps=0)
    assert line(rig) == [("RENEWING", None)]
    rig.clock.advance(24 * 3600)                                        # the next evening's slot
    rig.orchestrator.script(RESEARCH_MARKER, NO_CANDIDATES)
    await rig.cycle().run_due_slot()
    assert rig.rows("SELECT candidate_id, kind, prior_version_digest FROM ai_research_candidates") == [
        (renewal_candidate_id(V1), "RENEWAL", V1)]
    assert len(rig.registry.sent("get_ai_deployment_version")) == 1     # a RENEWING line is not read again


@pytest.mark.asyncio
async def test_a_case_for_another_version_is_a_wire_error(rig):
    seed_live_line(rig)
    await renewal_night(rig, prior=V2)                                  # the summary names another version
    assert rig.rows("SELECT state FROM ai_research_candidates") == [("SUBMITTED",)]
    assert rig.jev.requests == []


INITIAL_PICK = json.dumps({"candidates": [{"strategy": "S1", "universe": "U1", "bar_size": "B3",
                                           "points": [{"ENTRY_MINUTE": 615}], "thesis": "same evening"}]})


@pytest.mark.asyncio
async def test_an_initial_cap_refusal_leaves_the_renewal_eligible(rig):                  # PR #91 4218219455
    seed_live_line(rig)
    renewal_replies = [refused("FORWARD_EVIDENCE_PENDING", request_id=RENEWAL_REQUEST, retryable=True),
                       submitted(request_id=RENEWAL_REQUEST, state="DONE")]

    def submit(body):                       # either order: the renewal is still NEW when the INITIAL is refused
        if body["kind"] == "INITIAL":
            return refused("EVALUATION_LIMIT_REACHED")
        return renewal_replies.pop(0) if len(renewal_replies) > 1 else renewal_replies[0]
    await renewal_night(rig, submit=[submit], pumps=8, proposal=INITIAL_PICK)
    assert rig.rows("SELECT kind, end_code FROM ai_research_candidates ORDER BY kind") == [
        ("INITIAL", "REFUSED_EVALUATION_LIMIT_REACHED"), ("RENEWAL", "JUDGED_DEPLOY")]
    (record,) = rig.registry.sent("record_backtest_judgment")
    assert (record["kind"], record["renewal_of_version"]) == ("RENEWAL", V1)
    assert line(rig) == [("ENDED", "RENEWED")]
    assert rig.rows("SELECT state, version_digest, line_state FROM ai_research_registrations "
                    "WHERE kind = 'RENEWAL'") == [("REGISTERED", V2, "LIVE")]


@pytest.mark.asyncio
async def test_a_renewing_line_closed_without_a_judgment_is_asked_again_at_the_next_slot(rig):
    seed_live_line(rig)
    await renewal_night(rig, pumps=0)                                   # RENEWING, candidate NEW
    rig.store.db.execute("UPDATE ai_research_candidates SET state = 'CLOSED', "     # what the old cap close did
                         "end_code = 'NOT_SUBMITTED_EVALUATION_LIMIT_REACHED'")
    rig.clock.advance(24 * 3600)                                        # the next evening's slot
    rig.orchestrator.script(RESEARCH_MARKER, NO_CANDIDATES)
    cycle = rig.cycle()
    await cycle.run_due_slot()
    assert rig.rows("SELECT state, end_code FROM ai_research_candidates") == [("NEW", None)]
    for _ in range(6):
        await cycle.pump()
        rig.clock.advance(31)
    assert line(rig) == [("ENDED", "RENEWED")]
    assert len(rig.registry.sent("get_ai_deployment_version")) == 1     # reopening reads no version
```

In `tests/ai/research/test_research_slot_cycle.py`, delete Plan 4's `test_an_expired_or_withdrawn_version_ends_its_line_without_a_renewal`; this file's `test_a_withdrawn_or_ended_version_ends_its_line_without_a_renewal` replaces it, and EXPIRED now renews.

- [ ] **Step 3: Run the tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/ai/research/test_research_renewal.py -q --timeout=60`
Expected: FAIL with `ImportError: cannot import name 'renewal_candidate_id' from 'trader.ai.research_cycle'`.

- [ ] **Step 4: Judgment id and body by kind** (`trader/ai/backtest_judge.py`)

```python
def judgment_id_for(case_digest: str, kind: str = "INITIAL") -> str:
    """One judgment per case (spec 5.2 item 2): the id follows from the kind and the case, so a retry reuses it."""
    if kind not in ("INITIAL", "RENEWAL"):
        raise ValueError(f"unknown judgment kind {kind!r}")
    return "jdg-" + hashlib.sha256(f"{kind}|{case_digest}".encode("utf-8")).hexdigest()[:32]
```

In `judgment_body`, after the DEPLOY check:

```python
    kind, prior = case.summary.kind, case.summary.prior_version_digest
    if (kind == "RENEWAL") != (prior is not None):
        raise ValueError("a RENEWAL judgment names its prior version; an INITIAL one names none")
```

and use `"kind": kind, "renewal_of_version": prior` in the returned body.

`trader/ai/research_roles.py`: in `BACKTEST_SYSTEM`, after "REJECT cools the strategy down. " insert `"A RENEWAL case shows the forward replay and paper trips of an expired deployment: DEPLOY renews it on the same signed bundle, SHADOW or REJECT ends it. "`.

- [ ] **Step 5: The slot asks for renewals** (`trader/ai/research_cycle.py`)

Module level:

```python
RENEWAL = "RENEWAL"
LINE_ENDING_STATES = frozenset({"WITHDRAWN", "ENDED"})


def renewal_candidate_id(prior_version_digest: str) -> str:
    """Ruling 15: one renewal candidate per version, whatever the cycle."""
    return "rr-" + _sha(f"RENEWAL|{prior_version_digest}")[:32]
```

In `_run_slot`, call `await self._end_finished_lines(slot)`. Replace `_end_finished_lines`:

```python
    async def _end_finished_lines(self, slot: ResearchSlot) -> None:
        """An EXPIRED version asks for a renewal (the line becomes RENEWING); WITHDRAWN or ENDED ends the line."""
        await self._store.atransaction(self._reopen_stranded_renewals_in_tx)
        rows = await self._store.aquery("SELECT version_digest, strategy_key FROM ai_research_registrations "
                                        "WHERE state = 'REGISTERED' AND line_state = 'LIVE'")
        for version, strategy_key in rows:
            try:
                reply = parse_reply(VersionReply, "get_ai_deployment_version", await self._registry.call(
                    "get_ai_deployment_version", {"version_digest": version}))
            except (*AWAY, RpcRefused, WireError) as exc:
                logger.warning("deployment version %s unreadable (%s); its line stays live for now", version, exc)
                continue
            if not reply.found:
                logger.error("deployment version %s is unknown to the trader", version)
            elif reply.version.state == "EXPIRED":
                await self._store.atransaction(lambda conn, v=version, k=strategy_key:
                                               self._request_renewal_in_tx(conn, slot, v, k))
            elif reply.version.state in LINE_ENDING_STATES:
                await self._store.atransaction(lambda conn, v=version, s=reply.version.state:
                                               self._end_line_in_tx(conn, v, s))

    def _reopen_stranded_renewals_in_tx(self, conn: Any) -> None:
        """Ruling 17: a RENEWING line whose candidate was closed without a judgment asks again (never silently ends)."""
        rows = conn.execute(
            "SELECT c.candidate_id, c.end_code FROM ai_research_registrations r "
            "JOIN ai_research_candidates c ON c.kind = 'RENEWAL' AND c.prior_version_digest = r.version_digest "
            "LEFT JOIN ai_backtest_judgments j ON j.candidate_id = c.candidate_id "
            "WHERE r.line_state = 'RENEWING' AND c.state = 'CLOSED' AND j.judgment_id IS NULL").fetchall()
        now = self._clock.now()
        for candidate_id, end_code in rows:
            logger.error("renewal candidate %s was closed (%s) without a judgment; asked again", candidate_id, end_code)
            conn.execute("UPDATE ai_research_candidates SET state = 'NEW', end_code = NULL, request_id = NULL, "
                         "accepted_at = NULL, next_try_at = ?, updated_at = ? WHERE candidate_id = ?",
                         [now, now, candidate_id])

    def _request_renewal_in_tx(self, conn: Any, slot: ResearchSlot, version: str, strategy_key: str) -> None:
        """The candidate shares the cycle with INITIAL ones, but Plan 4's cap close skips it (kind = 'RENEWAL')."""
        body = canonical_json({"kind": RENEWAL, "prior_version_digest": version})
        now = self._clock.now()
        conn.execute(
            "INSERT INTO ai_research_candidates (candidate_id, cycle_id, kind, strategy_key, prior_version_digest, "
            "body_json, body_sha256, state, next_try_at, created_at, updated_at) "
            "VALUES (?, ?, 'RENEWAL', ?, ?, ?, ?, 'NEW', ?, ?, ?) ON CONFLICT (candidate_id) DO NOTHING",
            [renewal_candidate_id(version), slot.cycle_id, strategy_key, version, body, _sha(body), now, now, now])
        conn.execute("UPDATE ai_research_registrations SET line_state = 'RENEWING', updated_at = ? "
                     "WHERE version_digest = ? AND line_state = 'LIVE'", [now, version])
        logger.info("deployment version %s expired; renewal requested", version)

    def _end_line_in_tx(self, conn: Any, version: str, code: str) -> None:
        conn.execute("UPDATE ai_research_registrations SET line_state = 'ENDED', error_code = ?, updated_at = ? "
                     "WHERE version_digest = ? AND line_state IN ('LIVE', 'RENEWING')",
                     [code, self._clock.now(), version])
        logger.info("deployment line of %s ended: %s", version, code)
```

- [ ] **Step 6: The pump handles a RENEWAL candidate** (`trader/ai/research_cycle.py`)

`_start_new`: select `candidate_id, cycle_id, strategy_key, body_json, prior_version_digest`; `start(candidate_id, cycle_id, strategy_key, body_json, prior)`; on `RpcRefused` call `await self._close_candidate(candidate_id, f"RPC_{exc.code}", prior)`; on a non-retryable refusal call `await self._submit_refused(candidate_id, cycle_id, strategy_key, reply.code, prior)`. `_submit_refused` gains `prior: Optional[str] = None` and, inside its transaction after `_close_in_tx`:

```python
            if prior is not None:
                self._end_line_in_tx(conn, prior, f"RENEWAL_REFUSED_{code}")
```

Add:

```python
    async def _close_candidate(self, candidate_id: str, code: str, prior: Optional[str]) -> None:
        """Close a candidate; a renewal candidate ends its line with the same reason."""
        def work(conn: Any) -> None:
            self._close_in_tx(conn, candidate_id, code)
            if prior is not None:
                self._end_line_in_tx(conn, prior, f"RENEWAL_{code}")
        await self._store.atransaction(work)
```

`_poll_submitted`: select `candidate_id, request_id, accepted_at, kind, prior_version_digest`; `poll(candidate_id, request_id, accepted_at, kind, prior)`; before storing an `EVALUATED` row:

```python
            if view.found and view.state in TERMINAL and view.case_digest and view.summary is not None:
                if (view.summary.kind, view.summary.prior_version_digest) != (kind, prior):
                    raise WireError(f"get_evaluation: {request_id} answers a {view.summary.kind} case of "
                                    f"{view.summary.prior_version_digest}, not this {kind} candidate")
```

and the `EVALUATION_FAILED_NO_CASE` and `EVALUATION_STALE` closes use `self._close_candidate(candidate_id, code, prior)`.

`_judge_evaluated`: `judgment_id = judgment_id_for(case_digest, case.summary.kind)`. `_open_judgment_in_tx` inserts the case's own kind and prior:

```python
        conn.execute("INSERT INTO ai_backtest_judgments (judgment_id, candidate_id, case_digest, kind, "
                     "prior_version_digest, menu_json, state, created_at, updated_at) "
                     "VALUES (?, ?, ?, ?, ?, ?, 'JUDGING', ?, ?)",
                     [judgment_id, candidate_id, case.case_digest, case.summary.kind,
                      case.summary.prior_version_digest, json.dumps(list(jev_menu(case))), now, now])
```

`_record_decided`: select `j.judgment_id, j.verdict, j.body_json, c.strategy_key, j.kind, j.prior_version_digest`; `record(judgment_id, verdict, body_json, strategy_key, kind, prior)`. In `work`, the REFUSED branch adds `if kind == RENEWAL: self._end_line_in_tx(conn, prior, f"RENEWAL_JUDGMENT_{receipt.code}")` before `return`; after the cooldown mirror, the DEPLOY insert becomes:

```python
                if kind == RENEWAL and verdict == "DEPLOY":
                    self._renewal_registration_in_tx(conn, judgment_id, prior, strategy_key, now)
                elif kind == RENEWAL:
                    self._end_line_in_tx(conn, prior, f"RENEWAL_{verdict}")
                elif verdict == "DEPLOY":
                    conn.execute("INSERT INTO ai_research_registrations (judgment_id, kind, strategy_key, state, "
                                 "next_try_at, created_at, updated_at) VALUES (?, 'INITIAL', ?, 'ATTESTING', ?, ?, ?) "
                                 "ON CONFLICT (judgment_id) DO NOTHING", [judgment_id, strategy_key, now, now, now])
```

Add:

```python
    def _renewal_registration_in_tx(self, conn: Any, judgment_id: str, prior: str, strategy_key: str,
                                    now: dt.datetime) -> None:
        """Ruling 11: no attestation; the line's registration body with the renewal judgment id."""
        found = conn.execute("SELECT bundle_digest, body_json FROM ai_research_registrations WHERE version_digest = ?",
                             [prior]).fetchone()
        if found is None or found[0] is None or found[1] is None:
            raise WireError(f"no registration body of {prior} to renew")      # rolled back; logged by _each
        body = canonical_json({**json.loads(found[1]), "judgment_id": judgment_id})
        conn.execute(
            "INSERT INTO ai_research_registrations (judgment_id, kind, prior_version_digest, strategy_key, "
            "bundle_digest, body_json, body_sha256, state, next_try_at, created_at, updated_at) "
            "VALUES (?, 'RENEWAL', ?, ?, ?, ?, ?, 'REGISTERING', ?, ?, ?) ON CONFLICT (judgment_id) DO NOTHING",
            [judgment_id, prior, strategy_key, found[0], body, _sha(body), now, now, now])
```

`_advance_registrations`: select `judgment_id, state, body_json, prior_version_digest`; `advance(judgment_id, state, body_json, prior)` attests only in `ATTESTING` (an INITIAL row) and calls `await self._register(judgment_id, body_json, prior)`. `_register` gains `prior: Optional[str]`; the `WAITING_CAP` branch is unchanged; the other two branches become one transaction:

```python
        def work(conn: Any) -> None:
            if isinstance(outcome, RegisterRefused):
                logger.error("registration of %s refused: %s", judgment_id, outcome.code)
                conn.execute("UPDATE ai_research_registrations SET state = 'REFUSED', error_code = ?, updated_at = ? "
                             "WHERE judgment_id = ?", [outcome.code, now, judgment_id])
                if prior is not None:
                    self._end_line_in_tx(conn, prior, f"RENEWAL_REGISTER_{outcome.code}")
                return
            conn.execute("UPDATE ai_research_registrations SET state = 'REGISTERED', base_digest = ?, "
                         "version_digest = ?, expiry_session = ?, line_state = 'LIVE', error_code = NULL, "
                         "updated_at = ? WHERE judgment_id = ?",
                         [outcome.base_digest, outcome.version_digest, outcome.expiry_session, now, judgment_id])
            if prior is not None:
                self._end_line_in_tx(conn, prior, "RENEWED")
        await self._store.atransaction(work)
```

`recover()` needs no change: `_decide` builds the body from the candidate's summary, so a renewal cut mid-call is recorded `NO_VERDICT` as a RENEWAL and `_record_decided` ends its line.

- [ ] **Step 7: Run the tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/ai/research/ tests/ai/runtime/test_controller.py -q --timeout=120`
Expected: PASS (Plan 4's INITIAL tests are unchanged: INITIAL ids, bodies and states are the same).

- [ ] **Step 8: Commit**

```bash
git add trader/ai/backtest_judge.py trader/ai/research_roles.py trader/ai/research_cycle.py \
  tests/ai/research/cases.py tests/ai/research/test_research_slot_cycle.py tests/ai/research/test_research_renewal.py
git commit -m "$(cat <<'EOF'
feat: renew expired ai deployment lines from the research cycle

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 8: Renewal end to end over signed RPC (spec 9 "Renewal version")

**Files:**
- Modify: `tests/ai/research/research_world.py` (Plan 4's world: renewal options and helpers)
- Create: `tests/ai/research/test_renewal_acceptance.py`

**Interfaces:**
- Consumes: Plan 4's `ResearchWorld` (`build`, `night`, `node_rows`, `trader_call`, `research_client`, `reviews`, `bundles`, `strategy_trials`, `strategy`, `node`, `world.served`, the research signer, store and artifacts root it builds), Plan 2's `StrategyNode` and `ai_instance_name`, Tasks 1–7.
- Produces: `ResearchWorld.build(..., deploy_expiry_sessions=20, renewals=False)`; helpers `evening(day)`, `morning(day, hour, minute)`, `version(digest) -> dict`, `record_forward_rows(judgment_id, sessions, *, status="COMPLETE")`, `opened_holdouts() -> int`, `forward_view(version) -> ForwardEvidenceView`, `cases_dir`, `research_signer`.

- [ ] **Step 1: Extend the world** (`tests/ai/research/research_world.py`)

- `build` gains `deploy_expiry_sessions: int = 20` and `renewals: bool = False`. The test `trader.yaml` writes `deploy_expiry_sessions` into `ai_paper.backtest_judge`; the research side's `BacktestJudgeConfig(strategy_allowlist=(KEY,), deploy_expiry_sessions=deploy_expiry_sessions)` is the `judge` used by `build_cohort_spec` and, when `renewals` is true, by `RenewalRequests(store=store, trader=trader_port, signer=signer, artifacts_root=artifacts, repo_root=repo, judge=judge, warmup_sessions=judge.shadow_warmup_sessions, incomplete_after_hours=ResearchServiceConfig().shadow_incomplete_after_hours, now=served.now)`, passed as `EvaluationService(..., renewals=...)`.
- New helpers:

```python
    def evening(self, day: dt.date) -> None:
        """17:00 New York on ``day``: inside that evening's research window (slot due from 16:30).

        If one run_session across several sessions trips SP1's session machinery (session-end ledger, kill
        monitor), step through each session between the last time and ``day`` first: the controller creates
        no slot for them, because research_slot(now) only knows the current one."""
        self.world.served.run_session(dt.datetime.combine(day, dt.time(17, 0), NEW_YORK).astimezone(dt.timezone.utc))

    def morning(self, day: dt.date, hour: int, minute: int) -> None:
        self.world.served.run_session(dt.datetime.combine(day, dt.time(hour, minute), NEW_YORK)
                                      .astimezone(dt.timezone.utc))

    def version(self, digest: str) -> dict:
        return self.trader_call("cli", "get_ai_deployment_version", {"version_digest": digest})["version"]

    def record_forward_rows(self, judgment_id: str, sessions, *, status: str = "COMPLETE") -> None:
        """Rows as the research replay sends them: signed as research through the real record_shadow_result."""
        judgment = self.trader_call("research", "get_backtest_judgment",
                                    {"judgment_id": judgment_id, "case_digest": None})["judgment"]
        for day in sessions:
            numbers = ({"reason": None, "pnl_usd": 6.0, "fees_usd": 1.0, "trades": 1, "end_equity_usd": 100_006.0}
                       if status == "COMPLETE" else
                       {"reason": "BARS_MISSING: fixture", "pnl_usd": None, "fees_usd": None, "trades": None,
                        "end_equity_usd": None})
            reply = self.trader_call("research", "record_shadow_result", {
                "judgment_id": judgment_id, "case_digest": judgment["case_digest"], "verdict": judgment["verdict"],
                "session_date": day.isoformat(), "status": status, "bar_size": "15 mins", **numbers})
            assert reply["status"] == "INSERTED", reply

    def opened_holdouts(self) -> int:
        return len(ExperimentRegistry(self.research_db).opened_holdout_windows("strategies/time_of_day.py",
                                                                                "TimeOfDay"))

    def forward_view(self, version: str) -> ForwardEvidenceView:
        reply = self.trader_call("research", "get_deployment_forward_evidence", {"deployment_version": version})
        assert reply["status"] == "FOUND", reply
        return ForwardEvidenceView.model_validate(reply["evidence"])

    @property
    def cases_dir(self) -> Path:
        return self.artifacts / "cases"                    # the directory the trader reads cases from
```

(Imports: `datetime as dt`, `pathlib.Path`, `zoneinfo.ZoneInfo`, `trader.research.experiment_registry.ExperimentRegistry`, `trader.research.forward_evidence_view.ForwardEvidenceView`, `trader.research.renewal_service.RenewalRequests`, `trader.research.service_config.ResearchServiceConfig`. `NEW_YORK = ZoneInfo("America/New_York")`; `self.research_db`, `self.artifacts` and `self.research_signer` are the research DB, artifacts root and `AttestationSigner` the world already builds for Plan 3's parts; name them so if Plan 4 named them otherwise.)

- [ ] **Step 2: Write the failing tests** (`tests/ai/research/test_renewal_acceptance.py`)

```python
"""SP2c spec 9 "Renewal version" over signed RPC: expiry -> forward evidence -> renewal case -> Jev -> record ->
register on the same bundle -> the strategy service loads a fresh instance. SP1's real coordinator, no IB."""
import datetime as dt
import json

import pytest

from tests.ai.decisions.test_flows_acceptance import loop_thread  # noqa: F401
from tests.ai.research.research_world import DEPLOY_PROPOSAL, KEY, RESEARCH_CONIDS, ResearchWorld
from tests.ai.research.rig import NARRATIVE, ruling
from trader.ai.research_roles import BACKTEST_MARKER, RESEARCH_MARKER
from trader.research.evaluation_case import write_evaluation_case
from trader.research.evaluation_request import EvaluationRequestBody, evaluation_request_id
from trader.research.renewal_case import build_renewal_case
from trader.strategy.ai_deployment_source import ai_instance_name

pytestmark = pytest.mark.timeout(300)
NO_CANDIDATES = json.dumps({"candidates": []})
SESSIONS = (dt.date(2026, 7, 20), dt.date(2026, 7, 21), dt.date(2026, 7, 22))   # the first version's sessions


async def deployed(rw):
    """Friday 2026-07-17 evening: Plan 4's INITIAL chain ends in a LIVE version."""
    rw.node.orchestrator.script(RESEARCH_MARKER, DEPLOY_PROPOSAL)
    rw.node.jev.script(BACKTEST_MARKER, ruling("DEPLOY"))
    await rw.night()
    (judgment_id, version), = rw.node_rows("SELECT judgment_id, version_digest FROM ai_research_registrations "
                                           "WHERE state = 'REGISTERED'")
    return judgment_id, version


async def renewal_evening(rw, day, verdict):
    rw.evening(day)
    rw.node.orchestrator.script(RESEARCH_MARKER, NO_CANDIDATES)
    rw.node.jev.script(BACKTEST_MARKER, ruling(verdict))
    await rw.night()


@pytest.mark.asyncio
async def test_a_renewal_deploy_on_the_same_bundle_gets_a_fresh_version(tmp_path, loop_thread, monkeypatch):
    rw = await ResearchWorld.build(tmp_path, loop_thread, monkeypatch, deploy_expiry_sessions=3, renewals=True)
    try:
        initial, v1 = await deployed(rw)
        assert (rw.version(v1)["first_session"], rw.version(v1)["expiry_session"]) == ("2026-07-20", "2026-07-22")
        trials, holdouts, bundles, reviews = rw.strategy_trials(), rw.opened_holdouts(), rw.bundles(), rw.reviews()
        rw.record_forward_rows(initial, SESSIONS)
        await renewal_evening(rw, dt.date(2026, 7, 23), "DEPLOY")

        (renewal, v2, prior, line, body_json), = rw.node_rows(
            "SELECT judgment_id, version_digest, prior_version_digest, line_state, body_json "
            "FROM ai_research_registrations WHERE kind = 'RENEWAL'")
        assert (prior, line) == (v1, "LIVE") and v2 != v1
        fresh = rw.version(v2)
        assert (fresh["kind"], fresh["base_digest"], fresh["prior_version_digest"], fresh["first_session"]) == (
            "RENEWAL", rw.version(v1)["base_digest"], v1, "2026-07-24")
        assert rw.version(v1)["state"] == "ENDED"                   # superseded: never active again (ruling 13)
        assert rw.node_rows("SELECT line_state, error_code FROM ai_research_registrations WHERE version_digest = ?",
                            [v1]) == [("ENDED", "RENEWED")]
        judged = rw.trader_call("cli", "get_backtest_judgment", {"judgment_id": renewal, "case_digest": None})
        binding = judged["judgment"]["binding"]
        assert (judged["judgment"]["kind"], binding["prior_deployment_version"], binding["stage"]) == (
            "RENEWAL", v1, "FORWARD_COMPLETE")
        # spec 9: a renewal opens no holdout and adds no trial; the bundle and its review are the line's
        assert (rw.strategy_trials(), rw.opened_holdouts(), rw.bundles(), rw.reviews()) == (
            trials, holdouts, bundles, reviews)

        same_day = rw.trader_call("ai_research", "register_ai_deployment", json.loads(body_json))
        assert same_day["outcome"]["version_digest"] == v2           # the same renewal registration twice

        rw.morning(dt.date(2026, 7, 24), 10, 1)
        rw.strategy.reconcile()
        assert rw.strategy.instances() == {v2: ai_instance_name(v2)} and ai_instance_name(v2) != ai_instance_name(v1)
        next_day = rw.trader_call("ai_research", "register_ai_deployment", json.loads(body_json))
        assert (next_day["outcome"]["version_digest"], next_day["outcome"]["created"]) == (v2, False)
    finally:
        rw.close()


@pytest.mark.asyncio
async def test_a_renewal_after_the_bundle_expired_is_refused(tmp_path, loop_thread, monkeypatch):
    monkeypatch.setattr("trader.research.attest_export.ATTESTATION_LIFETIME", dt.timedelta(days=4))
    rw = await ResearchWorld.build(tmp_path, loop_thread, monkeypatch, deploy_expiry_sessions=3, renewals=True)
    try:
        initial, v1 = await deployed(rw)                             # bundle valid until Tuesday 07-21 ~16:32 ET
        assert rw.version(v1)["expiry_session"] == "2026-07-20"      # capped by the bundle (Plan 2 ruling 5)
        rw.record_forward_rows(initial, SESSIONS[:1])
        jev_calls = len(rw.node.jev.requests)
        await renewal_evening(rw, dt.date(2026, 7, 21), "DEPLOY")
        assert rw.node_rows("SELECT state, end_code FROM ai_research_candidates WHERE kind = 'RENEWAL'") == [
            ("CLOSED", "REFUSED_BUNDLE_EXPIRED")]
        assert rw.node_rows("SELECT line_state, error_code FROM ai_research_registrations WHERE version_digest = ?",
                            [v1]) == [("ENDED", "RENEWAL_REFUSED_BUNDLE_EXPIRED")]
        assert len(rw.node.jev.requests) == jev_calls                # Jev was not asked

        view = rw.forward_view(v1)                                   # a direct signed caller, same answer
        assert (view.renewable.ok, view.renewable.code) == (False, "BUNDLE_EXPIRED")
        case = build_renewal_case(view, created_at=rw.world.served.now(), warmup_sessions=5)
        digest = write_evaluation_case(rw.cases_dir, case, rw.research_signer)
        body = {"judgment_id": "jdg-renewal-direct", "case_digest": digest, "kind": "RENEWAL",
                "renewal_of_version": v1, "verdict": "DEPLOY", "menu": ["DEPLOY", "SHADOW", "REJECT"],
                "jev_model": "vendor/jev-1", "jev_attempt_ref": "att-direct",
                "decided_at": rw.world.served.now().isoformat(), "narrative": NARRATIVE}
        refused = rw.trader_call("ai_research", "record_backtest_judgment", body)
        assert (refused["status"], refused["code"]) == ("REFUSED", "BUNDLE_EXPIRED")
        shadow = rw.trader_call("ai_research", "record_backtest_judgment",
                                {**body, "verdict": "SHADOW", "narrative": None})
        assert shadow["status"] == "RECORDED" and rw.version(v1)["state"] == "ENDED"
    finally:
        rw.close()


@pytest.mark.asyncio
async def test_a_renewal_reject_cools_the_key_down_and_ends_the_line(tmp_path, loop_thread, monkeypatch):
    rw = await ResearchWorld.build(tmp_path, loop_thread, monkeypatch, deploy_expiry_sessions=3, renewals=True)
    try:
        initial, v1 = await deployed(rw)
        rw.record_forward_rows(initial, SESSIONS[:2])                # 07-22 never gets a row
        await renewal_evening(rw, dt.date(2026, 7, 23), "REJECT")
        (judgment_id, menu, verdict, state), = rw.node_rows(
            "SELECT judgment_id, menu_json, verdict, state FROM ai_backtest_judgments WHERE kind = 'RENEWAL'")
        assert (json.loads(menu), verdict, state) == (["SHADOW", "REJECT"], "REJECT", "RECORDED")
        judged = rw.trader_call("cli", "get_backtest_judgment",
                                {"judgment_id": judgment_id, "case_digest": None})["judgment"]
        assert judged["binding"]["stage"] == "FORWARD_INCOMPLETE" and judged["cooldown_until_session"] is not None
        assert rw.version(v1)["state"] == "ENDED"
        assert rw.node_rows("SELECT line_state, error_code FROM ai_research_registrations WHERE version_digest = ?",
                            [v1]) == [("ENDED", "RENEWAL_REJECT")]
        assert rw.node_rows("SELECT strategy_key, source FROM ai_research_cooldowns") == [(KEY, "REJECT")]
        again = rw.research_client("ai_research", "command").call(
            "submit_evaluation", {"kind": "RENEWAL", "prior_version_digest": v1}, dict)
        assert again["status"] == "DUPLICATE"                        # one renewal case per version
        body = EvaluationRequestBody.model_validate({
            "strategy_key": KEY, "cohort": [{"ENTRY_MINUTE": 615, "EXIT_MINUTE": 660}],
            "conids": sorted(RESEARCH_CONIDS), "bar_size": "15 mins", "research_day": "2026-07-23"})
        claim = rw.trader_call("research", "claim_evaluation",       # the trader's claim is the authority
                               {"request_id": evaluation_request_id(body), "body": body.model_dump()})
        assert (claim["status"], claim["code"]) == ("REFUSED", "FAMILY_COOLING_DOWN")
        rw.morning(dt.date(2026, 7, 24), 10, 1)
        rw.strategy.reconcile()
        assert rw.strategy.instances() == {}
    finally:
        rw.close()
```

- [ ] **Step 3: Run the tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/ai/research/test_renewal_acceptance.py -q --timeout=300`
Expected: FAIL with `TypeError: build() got an unexpected keyword argument 'deploy_expiry_sessions'`.

- [ ] **Step 4: Implement Step 1's world changes.** If a Plan 1–4 helper has another name, adapt only `research_world.py` and record the mapping in its docstring; never weaken an assertion.

- [ ] **Step 5: Run the tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/ai/research/test_renewal_acceptance.py tests/ai/research/test_research_acceptance.py -q --timeout=300`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add tests/ai/research/research_world.py tests/ai/research/test_renewal_acceptance.py
git commit -m "$(cat <<'EOF'
test: prove deployment renewal end to end over signed rpc

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 9: Docs, runbook and the full suite

**Files:**
- Modify: `docs/OPERATIONAL_STATE.md` (Plan 4's "AI research cycle (SP2c)" section), `docs/ARCHITECTURE.md` (the `ai_paper` deployment paragraph)

- [ ] **Step 1: Update the runbook.** In Plan 4's section, replace the "Known limits" sentence on renewal with a **Renewal** item, plain English, short:
  - A DEPLOY runs for `deploy_expiry_sessions` sessions. The evening after its last session the `ai` service asks the `research` service for a renewal. The research service reads the version's forward evidence from the trader (`get_deployment_forward_evidence`: every session's shadow row and the paper trips) and signs a RENEWAL case. It opens no holdout, writes no trial and uses no daily slot.
  - Jev may DEPLOY only when every forward session is COMPLETE. A renewal DEPLOY is registered on the same bundle and gets a new deployment version that starts the next session; the old version shows `ENDED` and never trades again. SHADOW, REJECT or `NO_VERDICT` ends the line; REJECT also cools the strategy key down.
  - Refused before Jev (the line ends): `BUNDLE_EXPIRED` (the bundle attestation ran out; only a new evaluation with a new holdout can deploy again), `FAMILY_COOLING_DOWN`, `STRATEGY_NOT_ALLOWED` (the key left the allowlist), `STRATEGY_SOURCE_CHANGED`, `RENEWAL_LINE_ENDED`.
  - Codes in `ai_research_registrations.error_code`: `RENEWED`, `RENEWAL_<verdict>`, `RENEWAL_REFUSED_<code>`, `RENEWAL_JUDGMENT_<code>`, `RENEWAL_REGISTER_<code>`. Line states: `LIVE`, `RENEWING`, `ENDED`.
  - Known limits: one session without an active version between a line and its renewal (ruling 1); a version registered a day after its judgment cannot be renewed with DEPLOY (ruling 4); a Jev outage during a renewal ends the line.
- [ ] **Step 2: ARCHITECTURE.md.** One paragraph after the deployment-version paragraph: renewal only after expiry; forward evidence = sealed shadow rows by the version's judgment and session plus paper trips; one renewal judgment per version (`renewal_judgments`, migration 125); a renewal DEPLOY needs a valid bundle, no cooldown and a complete forward window, checked by research before Jev and by the trader at the judgment and at registration.
- [ ] **Step 3: Full suite** under the shared lock (index rule):

```bash
until mkdir /private/tmp/mmr-suite.lock 2>/dev/null; do sleep 30; done
.venv/bin/python -m pytest tests/ -q -n 8 --timeout=120 --ignore=tests/test_ibrx_async.py \
    -p no:cacheprovider --basetemp=/private/tmp/mmr-sp2c-05-suite; status=$?
chmod -R u+w /private/tmp/mmr-sp2c-05-suite; rm -rf /private/tmp/mmr-sp2c-05-suite
rmdir /private/tmp/mmr-suite.lock; exit $status
```

Without pytest-xdist: drop `-n 8` and use `--timeout=60`. Then `.venv/bin/python -m pytest tests/test_ibrx_async.py --timeout=30 -q` alone. Expected: all pass; read the summary line, do not trust a count in a doc. Fix only failures this plan caused, in the task that caused them, never by loosening an assertion.

- [ ] **Step 4: Commit**

```bash
git add docs/OPERATIONAL_STATE.md docs/ARCHITECTURE.md
git commit -m "$(cat <<'EOF'
docs: add the sp2c renewal runbook

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
EOF
)"
```

The PR body must say: "No new key, port or allow-list row. Journal migration 125 is applied on the next trader start. Renewals run only when `research.enabled` is true in `ai.yaml` and the `research` service runs. Owner rulings to confirm: Plan 5 rulings 1, 2, 4 and 10."

## Self-review

- **Spec 5.2 item 2 (RENEWAL):** DEPLOY only if the case names the prior version (Plan 1's `RENEWAL_VERSION_MISMATCH` plus `RENEWAL_CASE_MISMATCH`), its code checks passed (`renewal_forward_complete` and the trader's own `FORWARD_INCOMPLETE` check) and the bundle attestation is valid (`BUNDLE_EXPIRED`); one judgment per case and per version; REJECT cooldown (Plan 1, Tasks 1 and 3).
- **Spec 5.2 item 5:** a renewal DEPLOY seals a fresh version on the same base and bundle; the same judgment twice returns the same version; a later non-DEPLOY renewal judgment ends the line through the real `renewals_of` (Tasks 1, 3, 4, 8).
- **Spec 5.2 item 7:** `get_deployment_forward_evidence` returns the paper trips and shadow rows of one version (Task 2), research only (Task 4).
- **Spec 6.2 "Renewal after expiry":** the research service builds the case by code from the forward evidence, labels it forward evidence, signs it like any case; complete only when every forward session is complete; no holdout, no trial; refused after the bundle expires (Tasks 5, 6, 8).
- **Spec 9 "Renewal version":** new version digest, old version never active again, fresh instance, same renewal judgment twice → same version (Task 8 test 1). **Spec 9 "Holdout discipline" renewal lines:** no holdout, no trial; registers against the initial bundle while valid, refused after (Task 8 tests 1 and 2).
- **Spec 11:** no forward-performance threshold (ruling 10).
- **Names:** Plan 1 (`RenewalChecks`, `RenewalStatus`, `ForwardEvidenceSource`, `ForwardEvidenceRefused`, `renewal_forward_complete`, `BacktestJudgments`), Plan 2 (`DeploymentActivity.status`, `deployment_sessions`, `ResearchBundleCheck`, `judgment_reader_for`, `binding_differences`, `RENEWAL_PRIOR_INVALID`), Plan 3 (`submit_evaluation` RENEWAL shape, `evaluation_summary`, `ShadowIngest`, `shadow_window`, `ResearchStore`), Plan 4 (`ResearchCycle`, `judgment_id_for`, `registration_body`, `line_state`) are used as those plans define them.
- **Placeholders:** none; Task 8's world helpers name the Plan 4 attributes they rely on and say how to adapt if a merged name differs.
