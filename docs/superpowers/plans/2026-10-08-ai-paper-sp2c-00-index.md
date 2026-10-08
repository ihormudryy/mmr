# AI Paper Bot SP2c: Plan Index

**Spec:** `docs/superpowers/specs/2026-10-08-ai-paper-sp2c-backtest-judge-design.md`
(merged as 727d56e2; owner-approved, reviewed by OpenAI and Grok). Ticket #36.
The spec's §5.1 method table is the binding list of RPC methods, servers and callers.
Its §3 words (strategy key, evaluation, parameter trial, Sharpe observation,
judgment, deployment version) are used exactly.

## Plans and order

| # | Plan file | Scope | Spec | Depends on |
|---|-----------|-------|------|------------|
| 1 | `2026-10-08-ai-paper-sp2c-01-claims-judgments.md` | Trader: evaluation claims, judgments, limits, cooldown, config `ai_paper.backtest_judge`, the signed case format, `get_backtest_judgment` by id or case digest, the renewal and forward-evidence ports (defaults refuse) | 5.2 (claims, judgments, limits), 6.1, 8, 9 | master |
| 2 | `2026-10-08-ai-paper-sp2c-02-registration-versions.md` | Trader + strategy service: registration bound to a DEPLOY judgment and a verified bundle, deployment versions (INITIAL and RENEWAL codes), withdraw, active deployments, signal version binding, recheck at admission and dispatch | 5.2 (registration, versions), 5.4, 8, 9 | 1 |
| 3 | `2026-10-08-ai-paper-sp2c-03-research-service.md` | `research` service: identity/ports/keys/compose, submit/get evaluation through the trader claim, cohort evaluation, evaluation cases, `attest_from_judgment` (with the bundle binding), nightly shadow replay, `record_shadow_result` + shadow books | 5.1, 6.2 (INITIAL), 7, 8, 9 | 1; 2 for shadow version rows |
| 4 | `2026-10-08-ai-paper-sp2c-04-research-cycle.md` | `ai` controller research cycle and Jev judgment workflow; ends expired lines; end-to-end acceptance; docs | 5.3, 8, 9 | 1, 2, 3 |
| 5 | `2026-10-08-ai-paper-sp2c-05-renewal.md` | Trader: real `ForwardEvidenceSource` (sealed `shadow_results` by the version's judgment, paper trips), `RenewalGate`, real `RenewalChecks`, one renewal judgment per version and `renewals_of`, wiring in `_build_ai_paper_parts`; research: RENEWAL case builder and the RENEWAL kind of `submit_evaluation`; controller: EXPIRED → renewal request (`RENEWING`), renewal registration without attestation; §9 renewal tests over signed RPC | 5.2 (renewal), 6.2 "Renewal after expiry", 9 "Renewal version", 11 | 1, 2, 3, 4 |

Plans 1 and 3's service skeleton can start in parallel; each plan is one PR.

**Who owns a name (rule R0).** The plan that builds the server side of a method owns its wire shape and names; the others align to it. Plan 1: the request body (`research_day`), the evaluation case module, the claim and judgment methods, `ai_paper.backtest_judge`. Plan 2: register / withdraw / version / active methods, the deployment version store, the entry refusal codes. Plan 3: `submit_evaluation`, `get_evaluation`, `attest_from_judgment`, `record_shadow_result`, the research ports. Plan 4: the controller config and `ai.duckdb` only. Plan 5 wires renewal end to end on top of these names and owns the `evidence` shape of `get_deployment_forward_evidence` (`ForwardEvidenceView`).

## Migrations

| Database | Plan 1 | Plan 2 | Plan 3 | Plan 4 | Plan 5 |
|----------|--------|--------|--------|--------|-------------------|
| Trader journal (SP1/SP2 use ≤ 100) | 110 `evaluation_claims`, 111 `backtest_judgments` (112–114 free) | 115 `ai_deployment_versions`, 116 `ai_deployment_withdrawals` (117–119 free; edits SP1's 56 in place) | 120 `shadow_results` (121–124 free) | — | 125 `renewal_judgments` (126–129 free) |
| `ai.duckdb` (SP2 uses 1–22) | — | — (edits SP2's migration 14 in place) | — | 30–34 (35–39 free) | — (40–49 stay free; Plan 4's tables already hold RENEWAL rows) |
| Research DB (today ≤ 11) | — | — | 20 `research_requests`, 21 `research_cases`, 22 `shadow_members`, `shadow_sent` | — | — (a renewal request is a `research_requests` row) |

No number is used twice. No legacy data (owner rule): edit CREATEs in place, no ALTER/backfill.

## Owner rulings to confirm

- Plan 1 ruling 12: the trader binds `research.pub`; run `./docker.sh -k` before deploying Plan 1, or `docker.sh` refuses to start.
- Plan 1 ruling 7: a REJECT cooldown counts sessions from the trader's own New York day and its length is fixed when recorded.
- Plan 2 ruling 1: the registration command id is a hash of the body plus the New York day; a refused registration stays refused for the rest of that day.
- Plan 2, no-legacy-data rule: three CREATEs change in place (journal 56, `strategy_signal_record`, `ai.duckdb` 14); these databases must start fresh.
- Plan 2 ruling 15: when a deployed strategy file changes after load, all its signals (SELL too) are dropped; exits go through the trader's brackets and controller closes.
- Plan 3 ruling 1: the research service sets `research_day`; a submit reply lost just before New York midnight and resent after it takes a second slot.
- Plan 3 ruling 14: the signing key is hidden in every container except `research`; `docker compose run cli research attest bundle` stops working (the host command still works).
- Plan 2 ruling 18: the SP1 acceptance harness no longer registers a fixture deployment; it takes an operator-given judged `deployment_version`.
- Plan 3 ruling 13: `research` mounts `mmr_db_data` read-write (bar reads need it), so the journal file is visible in that volume.
- Plan 3 ruling 3 (spec 11 open question): evaluation defaults live in a new top-level `research_service:` block of `trader.yaml`, not in `research/example_spec.yaml`.
- Plan 3 ruling 20 (spec 11 open question): one evaluation at a time, queue bounded by `queue_max`.
- Plan 4 ruling 3: research may run while the experiment is PAUSED, KILLED or STOPPED (it trades nothing).
- Plan 1 ruling 11, Plan 3 ruling 17, Plan 4 ruling 17: renewal and forward evidence move to Plan 5 (controller ruling R7; was "owner to confirm" in the drafts).
- Plan 5 ruling 1: a version is renewed only after it expired, so one session runs without an active version between a line and its renewal.
- Plan 5 ruling 2: a renewal takes no evaluation claim and no daily slot (it runs no backtest and writes no trial).
- Plan 5 ruling 4: forward sessions outside the judgment's shadow window are `NOT_REPLAYED` and count as incomplete, so a version registered a day late cannot be renewed with DEPLOY.
- Plan 5 ruling 10 (spec 11 open question): no forward-performance threshold beyond complete forward data; Jev judges the numbers.

## Tests

- Per task: targeted pytest. Full suite once per plan in its last task, under the shared lock
  (`until mkdir /private/tmp/mmr-suite.lock ...; rmdir`), with `-n 8 --timeout=120` when
  pytest-xdist is installed (PR #89), else single-process `--timeout=60`.
- All without real model, IB or network calls. Acceptance tests use SP1's real coordinator
  over signed RPC; no always-approving risk gate.
