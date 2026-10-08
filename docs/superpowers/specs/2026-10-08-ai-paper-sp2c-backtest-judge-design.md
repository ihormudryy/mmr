# AI Paper Bot — SP2c: Jev as Backtest Judge — Design

Status: design approved section by section by the owner on 2026-10-08; this file is
the written spec for owner review. Ticket: #36. Builds on SP1
(`docs/superpowers/specs/2026-10-05-ai-paper-sp1-foundation-design.md`) and SP2a+b
(`docs/superpowers/specs/2026-10-07-ai-paper-sp2ab-autonomous-loop-design.md`, merged).
Prepares SP3 (AI-written strategies need this gate).

## 1. Goal

No strategy trades on paper under an AI deployment without a recorded, verifiable
Jev verdict that points to real, signed evidence. Jev rules **DEPLOY / SHADOW /
REJECT** on code-computed backtest evidence. Every judgment is a trial. Rejected
families cool down. Every judged strategy keeps running in a nightly forward
replay, so the scoreboard measures whether Jev's judgment adds value.

## 2. Owner decisions (2026-10-08, do not re-argue)

- **Candidates:** the orchestrator proposes them: an existing strategy file from
  `strategies/` (allowlist), parameters from that strategy's declared tunables,
  conids, bar size. The model writes no code, no spec and no statistics.
- **Rules first, Jev on top:** a candidate that fails any paper-v1 rule can never
  be DEPLOY (Jev may pick SHADOW or REJECT). A candidate that passes every rule may
  be DEPLOY, SHADOW or REJECT. The model can only narrow, never loosen.
- **Shadow:** nightly forward replay over each session's new bars with the same
  backtester, cost model and live rules; no live shadow trading.
- **Limits (moderate defaults, operator config only):** 10 evaluations per New
  York day; a REJECTed family cools down for 10 trading sessions; at most 3 active
  AI-judged DEPLOYs; a DEPLOY expires after 20 sessions.
- **Where research runs:** a separate `research` service (approach A).

## 3. Roles and authority

| Role | May | May not |
|---|---|---|
| Orchestrator (model) | Propose candidates within the allowlist, declared tunables and the discretionary scope | Write code, specs, YAML or statistics; choose limits |
| Jev (model, OpenRouter only) | Rule DEPLOY / SHADOW / REJECT on a code-built `BacktestCase` | See the orchestrator's text; DEPLOY a rule-failing candidate |
| `ai` controller (code) | Schedule the research cycle, enforce limits before submitting, journal and replay model calls, record verdicts | Produce or alter evidence |
| `research` service (code) | Build the evaluation spec, run `research evaluate`, sign bundles, run shadow replays | Talk to the broker; call models |
| Trader (code) | Verify bundles, bindings, limits, cooldowns and expiry; own the judgment record | — |
| Strategy service (code) | Load active AI deployments with exact file-hash binding | Load anything the trader has not marked active |

## 4. Components

### 4.1 `research` service (new container, `trader/research_service.py`)

- Own RPC principal **`research`** with its own Ed25519 key pair (Plan 2 rules).
- Holds the research database, the bundle-signing key (separate from RPC keys, as
  today) and read-only access to price history. No broker access, no model
  credentials, no trader journal.
- Typed RPC (allow-list in `trader/messaging/principals.py`):
  - `submit_evaluation` (command, `ai_research` only): request names a strategy
    file on the allowlist, a class, parameters (declared tunables only, typed),
    conids, bar size and a client request id (idempotent). The service builds the
    evaluation spec itself (period, folds, embargo, holdout, cost model, notional
    from operator config) and returns an evaluation id.
  - `get_evaluation` (query, `ai_research` only): state (`QUEUED`, `RUNNING`,
    `DONE`, `FAILED`), the code-computed summary (paper-v1 rule results, cost
    stress 1x/1.5x/2x, deflated Sharpe, trial count of the family, holdout
    result) and, when sealed and signed, the bundle digest.
- Runs evaluations one at a time (CPU-bound; a bounded queue). Every finished
  evaluation, succeeded or failed, is a trial in `ExperimentRegistry` and counts
  in the family's deflated Sharpe.
- Nightly shadow replay (section 6).

### 4.2 Trader changes

1. **Verified evidence on registration.** `register_ai_deployment` (existing,
   `ai_research`) requires `evidence_ref` to be a bundle digest of a bundle the
   trader can read (`artifacts/sha256_<digest>/`, read-only mount). The trader
   verifies the signature against `~/.config/mmr/keys/verify/*.pem`, runs
   `require_qualified_research_evidence`, checks that the bundle binds to the
   deployment (strategy file hash, class, parameters, conids, bar size) and is
   not expired. Failure → refused with a specific code.
2. **Rules first.** `decider_verdict = DEPLOY` is accepted only if the bundle
   passed every paper-v1 rule.
3. **`record_backtest_judgment`** (command, `ai_research`): records every verdict
   (DEPLOY, SHADOW, REJECT, and `NO_VERDICT` when Jev failed) with the evaluation
   id, bundle digest (if any), family, Jev attempt reference and decided time.
   Idempotent by judgment id. A REJECT starts the family's cooldown.
4. **Limits, enforced by the trader:** daily evaluation count (New York day),
   family cooldown, active-DEPLOY cap, DEPLOY expiry. A DEPLOY registration or a
   judgment that breaks a limit is refused with its own code
   (`EVALUATION_LIMIT_REACHED`, `FAMILY_COOLING_DOWN`, `DEPLOY_CAP_REACHED`).
5. **`get_active_ai_deployments`** (query, `strategy` only): active, unexpired
   DEPLOY deployments with their strategy file hash, class, parameters, conids and
   bar size.
6. **`record_shadow_result`** (command, `research` only): idempotent nightly
   per-strategy, per-verdict result rows (section 6).

### 4.3 `ai` controller (research cycle)

- Runs after the session close (`ResearchSlot`), never during the entry window.
- Orchestrator proposes candidates; code drops any outside the allowlist,
  undeclared tunables, out-of-scope conids, cooling-down families, or over the
  daily limit; then `submit_evaluation`, poll `get_evaluation` until done.
- Builds `BacktestCase` from the code-computed summary only. If any paper-v1 rule
  failed, Jev's menu is SHADOW / REJECT; otherwise DEPLOY / SHADOW / REJECT.
- Malformed, off-menu or refused output → `NO_VERDICT` (recorded, never DEPLOY).
- Records the judgment; on DEPLOY registers the deployment with the bundle digest.
- Same daily model budget and journal as SP2a; judgments are replayable.

### 4.4 Strategy service

- A second strategy source: on reconcile it reads `get_active_ai_deployments`
  and loads each one whose file bytes hash to the recorded digest (refused
  otherwise, as SP1 already does); unloads withdrawn or expired ones.
- Their BUY signals enter the normal SP2 loop, where Jev judges every ENTER.

## 5. Limits and lifecycle

Config `ai_paper.backtest_judge` in `trader.yaml` (operator only; no env
overrides; no AI principal can change it):

| Key | Default |
|---|---|
| `evaluations_per_day` | 10 |
| `family_cooldown_sessions` | 10 |
| `max_active_deploys` | 3 |
| `deploy_expiry_sessions` | 20 |
| `strategy_allowlist` | explicit list of `strategies/<file>.py:<Class>` |

- A family is strategy file + class (the same key `ExperimentRegistry` uses).
- An expired DEPLOY is unloaded; re-deploying needs a new evaluation on newer
  data. The existing "holdout opened once" rule still applies.

## 6. Shadow measurement

- After each session close (after bars are complete), the `research` service
  replays every judged strategy (DEPLOY, SHADOW and REJECT within their
  tracking window) over that session's new 1-minute bars with the same
  backtester, cost model and `PaperAutomationRules`.
- Results go to the trader with `record_shadow_result`; the scoreboard shows one
  book per verdict (never summed across verdicts), so DEPLOY vs REJECT forward
  results are visible.
- Missing or bad bars → that day's row is INCOMPLETE with a reason, never
  guessed. A REJECTed family is tracked for its cooldown period plus 10 sessions.

## 7. Failure handling

| Failure | Behaviour |
|---|---|
| `research` service down, evaluation fails or times out | Recorded as a failed trial; no verdict; nothing deployed |
| Jev down, budget refused, bad output | `NO_VERDICT` recorded; never a default DEPLOY; may retry within the daily limit |
| Bundle signature, binding or expiry fails at registration | DEPLOY refused with a specific code |
| Strategy file changed after evaluation | Hash mismatch: refused at load and at signal dispatch (SP1) |
| Limit or cooldown breached (even by a bypassing client) | Trader refuses with its code |
| Shadow bars missing | INCOMPLETE row with reason |

## 8. Testing

All without real model, IB or network calls.

- End to end: propose → evaluate (real research pipeline on fixture bars) →
  judge → record → register → strategy service loads → signal → Jev on the ENTER,
  on SP1's real coordinator over signed RPC.
- Rules first: a rule-failing candidate never offers DEPLOY to Jev; a direct
  DEPLOY registration with such a bundle is refused.
- Bundle tampering: changed parameters, conids, bar size or file; expired
  bundle; unknown signing key → refused.
- Limits enforced by the trader with the `ai` side bypassed (direct signed calls).
- Every evaluation, failed or not, is a trial in the deflated Sharpe count.
- Expiry unloads the strategy; a changed file is refused at load.
- Shadow books per verdict, including INCOMPLETE.
- Replay of a judgment: zero external calls; config or code mismatch → INCOMPLETE.

## 9. Out of scope

AI-written strategy code (SP3), AI risk-policy generation and wider discovery
(SP2d), Telegram commands (SP2e), live trading, short strategies.

## 10. Open questions

- Evaluation period, folds and order notional defaults for AI-proposed candidates
  (proposed: reuse `research/example_spec.yaml` defaults, configurable).
- Whether the `research` service may run more than one evaluation at a time on
  the owner's machine (default: one).
