# AI Paper Bot — SP2c: Jev as Backtest Judge — Design

Status: design approved section by section by the owner on 2026-10-08; this file is
the written spec for owner review. Revised after review round 1 on PR #90. Ticket:
#36. Builds on SP1 (`docs/superpowers/specs/2026-10-05-ai-paper-sp1-foundation-design.md`)
and SP2a+b (`docs/superpowers/specs/2026-10-07-ai-paper-sp2ab-autonomous-loop-design.md`,
merged). Prepares SP3 (AI-written strategies need this gate).

## 1. Goal

No strategy trades on paper under an AI deployment without a recorded, verifiable
Jev verdict that points to real, signed evidence. Jev rules **DEPLOY / SHADOW /
REJECT** on code-computed backtest evidence. Every evaluation counts its parameter
trials in the strategy's multiple-testing count. Rejected strategies cool down.
Every judged strategy keeps running in a nightly forward replay, so the scoreboard
measures whether Jev's judgment adds value.

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
- **Review for attestation (added 2026-10-08):** on paper, Jev's DEPLOY judgment
  is the required `llm` operator review for attestation (`research review submit
  --reviewer-kind llm` is allowed on paper today). The `research` service records
  that review from the durable DEPLOY judgment, then signs. Live still needs a
  human review (unchanged).

## 3. Words used in this spec

- **Strategy key:** strategy file + class, for example
  `strategies/x.py:Foo`. This is **not** the registry's `family_id`. A
  `family_id` is a digest of code, lock file, container, dataset, search space,
  cost model and protocol (`trader/research/artifact.py`), so each new parameter
  set is a new `family_id`. Cooldown, the trial count and holdout windows all use
  the strategy key. "Family" in section 2 means the strategy key.
- **Evaluation:** one accepted `submit_evaluation` request. It names one strategy
  key and a frozen cohort of 1 to `max_cohort_points` parameter points. The daily
  cap counts evaluations.
- **Parameter trial:** one row in `ExperimentRegistry` (a cohort point or one of
  its neighbour points). One evaluation creates several. Every trial that reaches
  a terminal state (succeeded, failed, invalid, timed out) counts in the
  multiple-testing denominator, `strategy_trials(file, class)`, across every
  `family_id` of that strategy key.
- **Sharpe observation:** a succeeded trial with a Sharpe value. Failed trials
  count in the denominator but give no Sharpe value.
- **Judgment:** one Jev verdict on one evaluation case (or one renewal case). A
  judgment is not a trial.
- **Evaluation case:** the signed, code-built record of one evaluation's result
  (section 4.1). It never authorizes trading.
- **Bundle:** the existing signed research bundle (`mmr research attest bundle`).
  Only a DEPLOY judgment can lead to one.

## 4. Roles and authority

| Role | May | May not |
|---|---|---|
| Orchestrator (model) | Propose candidates within the allowlist, declared tunables and the discretionary scope | Write code, specs, YAML, statistics or review text; choose limits |
| Jev (model, OpenRouter only) | Rule DEPLOY / SHADOW / REJECT on a code-built `BacktestCase`; on DEPLOY, write the review narrative fields | See the orchestrator's text; DEPLOY a rule-failing candidate; confirm the holdout |
| `ai` controller (code) | Schedule the research cycle, journal and replay model calls, send judgments and registrations | Produce or alter evidence; sign anything |
| `research` service (code) | Claim evaluation slots at the trader, run evaluations, sign evaluation cases, record the `llm` review from a durable DEPLOY judgment, sign DEPLOY bundles, run shadow replays | Talk to the broker; call models; sign without a durable DEPLOY judgment; sign on live |
| Trader (code) | Own the evaluation claims, the judgment record, limits, cooldowns, expiry and deployment versions; verify cases, bundles and bindings | — |
| Strategy service (code) | Load active AI deployments on paper with exact binding of the executing bytes, class, params, conids and bar size | Load anything the trader has not marked active; load on live |

## 5. Components

### 5.1 `research` service (new container, `trader/research_service.py`)

**Identity, ports and access.**

- New RPC principal **`research`** with its own Ed25519 key pair
  (`~/.config/mmr/keys/rpc/research.key|.pub`), made by `mmr keys init`
  (`./docker.sh -k`) and checked by `mmr keys check-mount research`
  (`./docker.sh -K`).
- It is a typed RPC server: **42106** typed query, **42107** typed command.
  Private Docker network only; not published on the host.
- `principals.py`: `research` joins `KNOWN_PRINCIPALS` and `SERVER_PRINCIPALS`
  (not `CLIENT_PRINCIPALS`: like `strategy`, it is a server that also calls the
  trader, not an SDK signer). `SERVER_ACCEPTS["research"] = {ai_research, cli}`.
  `CALLS["research"] = {trader}`; `CALLS["ai_research"]` and `CALLS["cli"]` gain
  `research`; `SERVER_ACCEPTS["trader"]` gains `research`.
- Service identity: `SERVICE_PRINCIPAL["research"] = "research"` in
  `principals.py`, so `service_rpc_files("research")` and `mmr keys check-mount
  research` know the service and its exact key files.
- Rotation: `_LONG_LIVED_SERVICE_PRINCIPALS["research"] = ("research",)` in
  `rpc_keys.py`. `RESTART_ON_ROTATE` is derived from it and from the peer maps,
  so rotating `research` restarts `research`, `trader` and `ai`, and rotating
  `trader`, `ai_research` or `cli` now also restarts `research`.
- Key generation needs no new list: `mmr keys init` and the key backup walk
  `KNOWN_PRINCIPALS`. `docker.sh` derives the required key files from the
  compose file (`_compose_rpc_key_files`), so the new service's binds are
  checked before any start. `KEYCHECK_SERVICES` in `docker.sh` (the `-K` gate)
  gains `research`.
- Every method has an allow-list entry, and each handler checks the principal
  again itself (as `ai_paper_actions.py` does). A method-limited client is not
  the authority; the server-side check is.

| Server | Kind | Method | Callers |
|---|---|---|---|
| research | command | `submit_evaluation` | `ai_research` |
| research | query | `get_evaluation` | `ai_research`, `cli` |
| research | command | `attest_from_judgment` | `ai_research` |
| trader | command | `claim_evaluation` | `research` |
| trader | query | `get_evaluation_claim` | `research` |
| trader | command | `update_evaluation_claim` | `research` |
| trader | query | `get_deployment_forward_evidence` | `research` |
| trader | command | `record_backtest_judgment` | `ai_research` |
| trader | query | `get_backtest_judgment` | `research`, `ai_research`, `cli`, `dashboard` |
| trader | command | `record_shadow_result` | `research` |
| trader | query | `get_active_ai_deployments` | `strategy` |
| trader | query | `get_ai_deployment_version` | `cli`, `dashboard`, `ai_supervisor`, `ai_research` |
| trader | command | `withdraw_ai_deployment` | `cli`, `dashboard` |
| trader | command | `register_ai_deployment` (existing) | `ai_research` |

`research` may call no trading method, no `submit_ai_paper_decision` and no
policy method.

**Mounts (Docker).**

| Container | Mounts |
|---|---|
| `research` | Its own `research.key` and `research.pub`; `trader.pub` (it calls the trader); `ai_research.pub` (its only command caller) and `cli.pub`; the research signing key from `~/.config/mmr/keys/private/` (read-only, separate from every RPC key); the research DB (its own, read-write); `~/.local/share/mmr/artifacts` (read-write, it exports bundles and cases); the bar history (read-only); the universe files, `trader.yaml` and `execution_costs.yaml` (read-only). No other service's private key, no IB credentials, no model credentials, no trader journal. |
| `trader` | Adds `research.pub` (RPC) and `~/.config/mmr/keys/verify/` (read-only). It reads bundles and cases from the artifacts directory and never writes them there. It never mounts the signing key. |
| `ai` | Adds `research.pub` (it calls the research server). |

**`submit_evaluation` (command, `ai_research`).**

1. The request names a strategy key on the allowlist, a cohort of 1 to
   `max_cohort_points` parameter points (declared tunables only, typed), conids
   and a bar size. The **request id** is the digest of the canonical request body;
   the caller cannot choose it.
2. The service checks the request by code: allowlist, declared tunables, conids in
   scope, bar size known, and a holdout window that is free (section 6.2). A
   request that fails here is refused and claims nothing.
3. The service calls the trader's **`claim_evaluation`** with the request id and
   the canonical body. The trader, in one transaction:
   - returns the existing claim if the same id and the same body exist (a retry);
   - refuses `EVALUATION_REQUEST_CONFLICT` if the id exists with another body;
   - refuses `FAMILY_COOLING_DOWN` if the strategy key is cooling down;
   - refuses `EVALUATION_LIMIT_REACHED` if the New York day already has
     `evaluations_per_day` claims;
   - otherwise writes the claim as `QUEUED` with its New York day.
4. Only after an accepted claim does the service queue the work. A refused claim
   runs nothing and writes no trial.
5. **Lost reply.** If the claim reply is lost, the service reads it back with
   `get_evaluation_claim(request_id)` before any retry. A retry with the same id
   and body never takes a second slot.
6. **Restart.** On start the service reads its own `QUEUED` and `RUNNING`
   evaluations and resumes them under the same claim. A `RUNNING` trial with no
   result is closed as failed by the existing registry rules (stale attempt), so
   it still counts.
7. **Outage before any trial starts.** The claim stays counted for its day (the
   safe choice), but no trial row exists, so the denominator does not change.
   Resuming the claim is not a new evaluation.
8. The service reports each claim state change (`RUNNING`, `DONE`, `FAILED`)
   with `update_evaluation_claim(request_id, state)`. This is a separate method,
   so it never meets the body-conflict rule of `claim_evaluation`. The trader
   only allows forward moves; a repeat of the current state is a no-op.

**Running an evaluation (cohort, then one holdout).** The service builds the
evaluation spec itself (period, folds, embargo, holdout, cost model and notional
from operator config). Today `evaluation.evaluate` runs the full cost stress
(1x, 1.5x, 2x) and the pre-holdout gate only for the one main point; its
neighbours run at 1x as sensitivity evidence; then it opens the main point's
holdout. SP2c splits this into three phases (an evaluator change):

1. **Pre-holdout, per cohort point.** Every cohort point is a selectable point.
   Each one runs exactly what the main point runs today: all walk-forward folds
   at 1x, 1.5x and 2x, its own neighbours at 1x, and the full pre-holdout
   paper-v1 gate (cost stress, deflated Sharpe with the strategy-key trial count,
   neighbour sensitivity, regimes, liquidity). Neighbours are never selectable.
2. **Select.** Among the points that pass the full pre-holdout gate, code picks
   one by a fixed rule on walk-forward evidence only (section 6.2). If none
   passes, the evaluation ends at the pre-holdout stage and seals no artifact.
3. **Holdout.** The registry seals the artifact with the selected point's trial
   as `selected_trial_id` and opens that point's holdout, once. No other point
   opens a holdout.

The whole cohort is one experiment family (its search space is the cohort plus
the neighbourhood). Evaluations run one at a time (CPU-bound; a bounded queue).

**Evaluation case (new, signed, never authorizing).** Every finished or failed
evaluation produces one evaluation case:

- Content: request id, claim day, strategy key, executing file hash, class,
  cohort, selected params, conids, bar size, each `family_id` and trial id, the
  stage reached (pre-holdout failure, holdout failure, complete), every paper-v1
  rule result for every cohort point, cost stress 1x/1.5x/2x, deflated Sharpe,
  `strategy_trials` count, the selected point, holdout result if opened, artifact
  id and eligibility decision digest if sealed, the `previously_revealed`
  sessions used, warm-up length.
- Signed with the research signing key under its own domain tag
  (`mmr.research.evaluation-case.v1`). It has no attestation, no review and no
  manifest, so the bundle verifier and `require_qualified_research_evidence`
  refuse it.
- Stored under `artifacts/cases/sha256_<digest>.json` (immutable).
- The current evaluator stops before the holdout when a pre-holdout rule fails
  and seals no artifact. The case is how such a candidate still reaches Jev for
  SHADOW or REJECT. A qualified bundle exists only after a DEPLOY.

**`get_evaluation` (query).** State (`QUEUED`, `RUNNING`, `DONE`, `FAILED`) and,
when done, the case digest and the code-computed summary. It never returns a
bundle digest before a DEPLOY judgment exists.

**`attest_from_judgment` (command, `ai_research`).** The review handoff:

1. Refused unless the account mode is paper.
2. Reads the judgment from the trader with `get_backtest_judgment`. The trader's
   record is the authority, not the caller's body.
3. Requires: verdict DEPLOY; the judgment's case digest is a case this service
   signed; the case is complete with a passed holdout and a `PAPER_ELIGIBLE`
   decision; the binding (strategy key, file hash, params, conids, bar size)
   matches the case.
4. Writes one `OperatorReview` for that artifact and decision:
   `reviewer_kind = "llm"`, `reviewer = <Jev model id>#<judgment id>`,
   `reviewed_at` = the judgment's decided time, the §8.5 narrative fields from
   the judgment, and `holdout_opened_once_confirmed` set by code from the
   registry (never by the model). If a different review already exists for that
   decision, it is refused (`REVIEW_CONFLICT`).
5. Runs `attest_and_export` (unchanged exporter, paper) and returns the bundle
   digest. A repeat call returns the same digest.

**Nightly shadow replay:** section 7.

### 5.2 Trader changes

1. **Evaluation claims.** `claim_evaluation`, `get_evaluation_claim` and
   `update_evaluation_claim` (section 5.1). Claims are durable rows keyed by request id, with body digest, strategy
   key, New York day, state and times.
2. **`record_backtest_judgment`** (command, `ai_research`). Body: judgment id,
   case digest, kind (`INITIAL` or `RENEWAL` with the deployment digest), verdict
   (DEPLOY, SHADOW, REJECT or `NO_VERDICT`), the menu offered, Jev attempt
   reference, decided time and, for DEPLOY, the §8.5 narrative fields. The
   trader:
   - verifies the case signature against `keys/verify/*.pem`;
   - for an `INITIAL` case, allows DEPLOY only if the case is complete, every
     paper-v1 rule passed and the holdout passed (rules first);
   - for a `RENEWAL` case, allows DEPLOY only if the renewal case names the prior
     deployment version, its code checks passed (section 6.2) and that
     deployment's bundle attestation is still valid; no holdout is involved;
   - allows one judgment per case; the same id with the same body returns the
     first receipt; another body or another id for the same case is refused
     (`JUDGMENT_CONFLICT`);
   - starts the strategy key's cooldown on REJECT.
3. **`get_backtest_judgment`** (query): the durable judgment by id.
4. **Registration bound to a DEPLOY judgment.** `register_ai_deployment` now
   takes the judgment id and the bundle digest. In one transaction the trader:
   - reads the durable judgment; refuses `JUDGMENT_MISSING` if absent and
     `JUDGMENT_NOT_DEPLOY` for SHADOW, REJECT or `NO_VERDICT`;
   - verifies the bundle signature and runs `require_qualified_research_evidence`;
   - checks that the bundle, the judgment's case and the deployment body name the
     same evaluation, artifact, `family_id`, file hash, class, params, conids and
     bar size, and that the bundle's review names this judgment (for a
     `RENEWAL`: the initial judgment of the prior deployment version the renewal
     names); any difference → `JUDGMENT_MISMATCH`;
   - refuses a second deployment for the same judgment (`JUDGMENT_ALREADY_BOUND`);
   - checks the active-DEPLOY cap (`DEPLOY_CAP_REACHED`), the cooldown and the
     bundle's expiry;
   - seals the base deployment (unchanged `AiDeployment` record) and then seals
     a new **deployment version** record (item 5).
   A signed caller cannot register after REJECT or without a judgment.
5. **Deployment version (new sealed record).** The `AiDeployment` digest
   (`ai_deployments.py`) covers only the strategy binding, verdict and bundle,
   and registering the same content returns the existing row. A renewal on the
   same bundle has the same content, so that digest cannot be the version.
   SP2c adds a separate insert-only table `ai_deployment_versions` (a new trader
   migration):
   - Body: base deployment digest, judgment id, kind (`INITIAL` or `RENEWAL`),
     prior version digest (null for `INITIAL`), first session, expiry session,
     and `binding_verified_by_bundle = true`.
   - Digest: `sha256` over a new domain tag `mmr.ai-deployment-version.v1` plus
     the canonical body. Every read recomputes it, like `get_sealed`.
   - One version per judgment (`JUDGMENT_ALREADY_BOUND`). A renewal always has a
     new judgment id and new sessions, so it always gets a fresh version digest;
     it never reuses the old one.
   - The base row keeps its SP1 meaning and its `CLAIMED_NOT_VERIFIED`
     provenance column. The bundle check is recorded on the version, which is
     the only thing SP2c treats as authority.
   - `register_ai_deployment` returns both digests. `get_ai_deployment` is
     unchanged; a new query `get_ai_deployment_version` (`cli`, `dashboard`,
     `ai_supervisor`, `ai_research`) reads a version. `SignalEntry`,
     `AiPaperDecision` (new field `deployment_version`, next to the existing
     `deployment_digest`), strategy instances and shadow rows carry the version
     digest.
   A version is **active** while its judgment stands, it is not withdrawn, the
   current session is not after its expiry session, and it is within the cap.
   `withdraw_ai_deployment` (operator) and expiry end it. A later non-DEPLOY
   renewal judgment also ends the line.
6. **`get_active_ai_deployments`** (query, `strategy`): each active version with
   its version digest, base deployment digest, strategy file path, file hash,
   class, params, conids, bar size and expiry session.
7. **`get_deployment_forward_evidence`** (query, `research`): the paper trips
   and the shadow rows of one deployment version, for a renewal case.
8. **Recheck at admission and at final dispatch.** For an AI-deployment ENTER,
   `submit_ai_paper_decision` today checks only a sealed DEPLOY verdict
   (`ai_paper_decision._deployment`). SP2c adds, at admission and again right
   before the order is sent: the signal's deployment version is active now
   (judgment, not withdrawn, not expired), the cap holds, the strategy key is
   not cooling down, and the signal's source digest equals the deployment's file
   hash. A failure refuses the ENTER (`DEPLOYMENT_NOT_ACTIVE`,
   `DEPLOYMENT_EXPIRED`, `FAMILY_COOLING_DOWN`, `STRATEGY_SOURCE_MISMATCH`).
   Exits, reductions and broker-proven closes never depend on these checks.
9. **`record_shadow_result`** (command, `research`): section 7.

### 5.3 `ai` controller (research cycle)

- Runs after the session close (`ResearchSlot`), never during the entry window.
- The orchestrator proposes candidates. Code drops any outside the allowlist,
  undeclared tunables, out-of-scope conids or cooling-down strategy keys, and
  groups each strategy key's points into one frozen cohort.
- `submit_evaluation`, then poll `get_evaluation` until done. A refusal from the
  trader's claim (limit, cooldown, conflict) ends that candidate.
- Builds `BacktestCase` from the signed case only. If the case did not pass every
  rule and the holdout, Jev's menu is SHADOW / REJECT; otherwise DEPLOY / SHADOW
  / REJECT.
- A DEPLOY must carry every §8.5 narrative field, each non-empty; code validates
  them. Malformed, off-menu, missing narrative or refused output → `NO_VERDICT`
  (recorded, never DEPLOY). Jev retries happen inside one judgment under SP2a's
  attempt rules; a recorded `NO_VERDICT` is final for that case.
- `record_backtest_judgment`. On DEPLOY: `attest_from_judgment`, then
  `register_ai_deployment` with the judgment id and the bundle digest.
- Same daily model budget and journal as SP2a; judgments are replayable.

### 5.4 Strategy service

This is new work. SP1 hashes the loaded bytes only for an armed paper-automation
bundle (`strategy_runtime._verify_artifact_at_load`). The AI-deployment path has
no hash check today (`ai_deployments.py` records `strategy_digest` as
`CLAIMED_NOT_VERIFIED`).

- **Paper only.** On a live account the service loads no AI deployment.
- **Load.** On reconcile it reads `get_active_ai_deployments`. For each one it
  reads the file once, hashes the exact bytes it will execute, and loads the
  class from those bytes. It loads only if the hash, class, params, conids and
  bar size all equal the deployment. The instance is keyed by the version
  digest; the name is derived from it, so two versions never share an instance.
  A renewal therefore loads a fresh instance.
- **Unload.** A deployment that is no longer active (withdrawn, expired, cap,
  new version) is unloaded on the next reconcile.
- **File replaced after load.** At each reconcile and before each signal the
  service re-hashes the file. On a mismatch it stops new entries from that
  instance and unloads it. Exits for positions already open still go through.
- **Signals.** `SignalEntry` gains `deployment_version` and `source_digest`.
  Every BUY from an AI-deployment instance carries both, so the trader can do the
  rechecks in 5.2 item 8. A signal from an instance with no verified binding is
  never emitted.
- Their BUY signals enter the normal SP2 loop, where Jev judges every ENTER.

## 6. Limits, holdout discipline and lifecycle

### 6.1 Config

Config `ai_paper.backtest_judge` in `trader.yaml` (operator only; no env
overrides; no AI principal can change it):

| Key | Default |
|---|---|
| `evaluations_per_day` | 10 |
| `family_cooldown_sessions` | 10 |
| `max_active_deploys` | 3 |
| `deploy_expiry_sessions` | 20 |
| `max_cohort_points` | 3 |
| `shadow_warmup_sessions` | 5 |
| `strategy_allowlist` | explicit list of `strategies/<file>.py:<Class>` |

- Cooldown and the trial count use the strategy key. A REJECT of
  `strategies/x.py:Foo` with one parameter set cools down every parameter set of
  that file and class. A new parameter set does not reset the trial count.

### 6.2 Holdout discipline

- **Freeze the cohort.** The cohort for one strategy key is fixed in the claim
  before any walk-forward run starts. No point can be added after a result is
  seen.
- **Select on walk-forward only.** Code picks the point with the best
  walk-forward evidence (highest deflated walk-forward Sharpe among points that
  pass the pre-holdout rules; ties by cohort order). Holdout data never feeds the
  choice.
- **Open one disjoint holdout once.** Only the selected point opens the final
  holdout, once. Its window must start after every holdout window already opened
  for the strategy key (the existing `opened_holdout_windows` guard). Those
  sessions are **revealed** for that strategy key forever.
- **Revealed sessions are never a future holdout.** A later evaluation of the
  same strategy key is refused before claiming (`HOLDOUT_NOT_AVAILABLE`) until
  enough new sessions exist after the last revealed window for a full disjoint
  holdout. Shifting the period by a day does not help: the new holdout must still
  lie after every revealed session.
- **Revealed sessions may be selection data.** A later evaluation may use
  previously revealed sessions in its walk-forward folds, so they can shape the
  next cohort and its selection. The case labels them `previously_revealed` and
  states how many holdouts the strategy key has opened before. The spec claims
  only that the new holdout is data no earlier holdout or fold has touched. It
  does **not** claim the new test is independent of earlier adaptive choices;
  the cross-family trial count (below) is the correction for that, and Jev sees
  both numbers.
- **Renewal after expiry.** A DEPLOY that expires may be renewed by a `RENEWAL`
  judgment. The `research` service builds its case by code: it reads the paper
  trips and shadow rows of that deployment version with
  `get_deployment_forward_evidence`, labels them forward evidence, and signs the
  case like any other case. The renewal case's code checks pass only if every
  forward session since the deployment started is complete (no INCOMPLETE row).
  It opens no holdout and creates no parameter trial. A renewal
  DEPLOY creates a new sealed deployment version (section 5.2 item 5) on the same
  base deployment and bundle while that bundle's attestation is still valid. When the bundle has expired, renewal is refused;
  only a new evaluation with a new disjoint holdout can deploy again.
- **Cross-family count.** Every terminal trial of every cohort counts in
  `strategy_trials(file, class)`, whatever its `family_id`.

## 7. Shadow measurement

- **Fixed cohort.** Each judgment joins the shadow cohort once, at its decided
  time. Its tracking window is fixed then: DEPLOY and SHADOW for
  `deploy_expiry_sessions`; REJECT for its cooldown plus 10 sessions. Later
  judgments never change an earlier member.
- **Replay.** After each session close (after bars are complete) the `research`
  service replays every member on **its own bound bar size**, with the same
  backtester, cost model and `PaperAutomationRules`. Each nightly run covers the
  first session after the verdict up to the new session, so the strategy state
  equals a continuous run; the new session's row is the change since the
  previous row.
- **Warm-up without trading.** Today `run_window_job` sets the starting capital
  and trades from `job.start`, so bars placed before the window would trade and
  book P&L. SP2c adds a backtester option `trading_start`: bars before it (the
  warm-up, `shadow_warmup_sessions` sessions, recorded in the case) only feed the
  strategy's state. Signals before `trading_start` are dropped, no order is
  simulated, no cost is charged, and equity starts at the initial capital at
  `trading_start` (the first post-verdict session).
- **Identity.** One row per (judgment id, deployment version or none, session
  date). `record_shadow_result` is idempotent by that identity; another body for
  the same identity is refused.
- **Books.** The scoreboard shows one book per verdict (never summed across
  verdicts), so DEPLOY vs REJECT forward results are visible.
- Missing, bad or wrong-size bars → that day's row is INCOMPLETE with a reason,
  never guessed.

## 8. Failure handling

| Failure | Behaviour |
|---|---|
| Trader refuses the claim (limit, cooldown, conflict) | Nothing runs; no trial; the candidate ends |
| Claim reply lost | Read back by request id; never a second slot |
| `research` service down before any trial starts | Claim stays counted for its day; no trial row; resumed under the same claim |
| Evaluation fails or times out after a trial started | Terminal trial rows count; a signed case records the failure; no DEPLOY possible |
| Jev down, budget refused, bad output | `NO_VERDICT` recorded; never a default DEPLOY |
| Attestation without a durable DEPLOY judgment, or on live | Refused; no review row, no bundle |
| Registration without a DEPLOY judgment, or a mismatch | Refused with a specific code |
| Bundle signature, binding or expiry fails at registration | Refused with a specific code |
| Strategy file changed after evaluation or after load | Not loaded, or entries stopped and unloaded; exits still go through |
| Deployment expires, is withdrawn or cools down after a signal | ENTER refused at admission or at final dispatch; exits unaffected |
| Shadow bars missing or wrong size | INCOMPLETE row with reason |

## 9. Testing

All without real model, IB or network calls.

- End to end: propose → claim → evaluate (real research pipeline on fixture
  bars) → case → judge → record → attest → register → strategy service loads →
  signal → Jev on the ENTER, on SP1's real coordinator over signed RPC.

One regression per review blocker:

- **Rule failure path (case record):** a pre-holdout failure produces a signed
  case and no artifact; Jev is offered SHADOW / REJECT only; the judgment is
  recorded; no bundle exists; the case digest used as `evidence_ref` is refused.
- **Review handoff:** a passing evaluation with no judgment → `attest_from_judgment`
  refused and no review row; after a DEPLOY judgment → one `llm` review with the
  judgment's narrative and code-set holdout confirmation, then attest and register
  pass; the same call on a live account is refused; a DEPLOY missing a narrative
  field is `NO_VERDICT`.
- **Registration bound to the judgment:** a direct signed registration after
  REJECT → `JUDGMENT_NOT_DEPLOY`; with no judgment → `JUDGMENT_MISSING`; a DEPLOY
  judgment of another evaluation, or other params, conids or bar size →
  `JUDGMENT_MISMATCH`; a second registration for one judgment →
  `JUDGMENT_ALREADY_BOUND`.
- **Daily cap at the trader:** two concurrent claims for the last slot → exactly
  one accepted; a lost reply after acceptance → readback returns the same claim
  and no second slot is used; the same id with another body →
  `EVALUATION_REQUEST_CONFLICT`; a restart with `QUEUED` and `RUNNING` work
  resumes it under the same claim; an outage before any trial starts writes no
  trial row; a direct `submit_evaluation` during a cooldown runs nothing.
- **Recheck at admission and dispatch:** expiry between signal and Jev → ENTER
  refused at admission; expiry or withdrawal between Jev and send → refused at
  final dispatch; an exit for the same conid in the same window still goes out.
- **Strategy binding:** a file replaced after load stops entries and unloads the
  instance while its exit still goes out; a changed file is refused at load; a
  signal without `deployment_version` is refused; nothing loads on live.
- **Holdout discipline:** a failed revealed holdout followed by a shifted-window
  evaluation of the same strategy key → `HOLDOUT_NOT_AVAILABLE`, no claim; the
  cohort's holdout is opened only for the walk-forward choice; a renewal opens
  no holdout and adds no trial; a renewal DEPLOY registers against the initial
  judgment's bundle while it is valid and is refused after it expires; trial
  counts carry across `family_id`s; a later evaluation whose folds include a
  revealed window marks those sessions `previously_revealed` in its case.
- **Cohort selection:** in a cohort where the point with the best 1x Sharpe
  fails the 2x cost stress, that point is not selected and a point that passes
  the full gate is; a neighbour with a better Sharpe than every cohort point is
  never selected; if no point passes, no artifact is sealed and no holdout opens.
- **Renewal version:** after expiry, a renewal DEPLOY on the same bundle returns
  a new version digest (not the old one); the old version stays expired; the
  strategy service loads a fresh instance for the new version; registering the
  same renewal judgment twice returns the same version.

Majors and other checks:

- **Counting:** one evaluation with one point and two neighbours adds three
  trials to `strategy_trials`; a judgment adds none; a REJECT of one parameter
  set cools down another parameter set of the same file and class.
- **Shadow:** a 15-minute candidate (the longest bar size the evaluator
  accepts) replays 15-minute bars, never 1-minute bars; a wrong-size bar →
  INCOMPLETE; with warm-up bars before the first post-verdict session, the
  forward row has no fill, no cost and no P&L before that session and starts at
  the initial capital; the nightly row equals the same session in one continuous
  run;
  books per verdict, including INCOMPLETE; a repeat row is a no-op, a changed one
  is refused.
- **Access:** every new method refused for every caller outside its table row,
  at the server; `research` cannot call any trading method; `check-mount research`
  fails if any other private key is mounted; `RESTART_ON_ROTATE["research"]`
  is `("ai", "research", "trader")`; `KEYCHECK_SERVICES` includes `research`.
- Bundle tampering: changed parameters, conids, bar size or file; expired
  bundle; unknown signing key → refused.
- Replay of a judgment: zero external calls; config or code mismatch → INCOMPLETE.

## 10. Out of scope

AI-written strategy code (SP3), AI risk-policy generation and wider discovery
(SP2d), Telegram commands (SP2e), live trading, short strategies.

## 11. Open questions

- Evaluation period, folds and order notional defaults for AI-proposed candidates
  (proposed: reuse `research/example_spec.yaml` defaults, configurable).
- Whether the `research` service may run more than one evaluation at a time on
  the owner's machine (default: one).
- Whether a renewal DEPLOY needs a forward-performance threshold beyond
  complete forward data (default: none; Jev judges the forward numbers).
