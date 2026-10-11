# AI Paper Bot SP3: Plan Index

**Spec:** `docs/superpowers/specs/2026-10-11-ai-paper-sp3-sandboxed-codegen-design.md`
(merged as db235415; owner-approved, reviewed by OpenAI and Grok; section 14 "Amendments" added with these
plans). Tickets: #37 (epic), #110 (these plans). The spec's section 3 words (authored strategy, catalogue
strategy, store, digest, session, authored key) are used exactly. The spec wins over a plan; a plan that finds a
conflict records a ruling and the owner confirms it below.

## Plans and order

| # | Plan file | Scope | Spec | Depends on |
|---|-----------|-------|------|------------|
| 1 | `2026-10-11-ai-paper-sp3-01-api-checker-benchmark.md` | The authored-strategy API, authored keys and digests (Task 1) and the ORB port fixture with the catalogue's `MIN_BARS` gate (Task 2), which the benchmark needs (Plan 1 ruling 2); then **the benchmark** (Task 3, its first measurement: streamed one-bar-at-a-time evaluation cost, decides the evaluation-length ruling; run in Task 7); then the module-attribute allow-list, the static checker, the escape corpus (incl. the nine runtime-hostile files Plan 2 loads) and the shipped pandas/numpy walk | 7.1, 7.2, 10 (escape corpus, static check, benchmark) | master |
| 2 | `2026-10-11-ai-paper-sp3-02-sandbox-service.md` | `sandbox` service: supervisor, protocol (`OPEN` with `conids`, `BAR` with `conid`, `OPEN_PROBE`), child process model (slot uids, rlimits, import guard, reduced built-ins, one strategy instance per conid), live pool 3 (one session per deployment) and research pool 4 (one conid each), sockets, Dockerfile users and build id, compose stanza, seccomp profile, the sandbox client, the isolation checks and posture used by `prove-isolation`, production-service and hostile tests | 7.5, 7.6 (client), 9, 10, 14 | 1 |
| 3 | `2026-10-11-ai-paper-sp3-03-trader-authority.md` | Trader: `ai_author` principal and key wiring, `ai_paper.authored_strategies` config (`max_active` 1-3), the store volume and records, the state machine and cap, `submit_authored_strategy` (tunables `{low, high, default}`) / `get_authored_strategies` / `list_authored_strategies`, state reports from research and strategy (retirement also withdraws the version), the isolation record keyed by build id, `mmr ai-author prove-isolation [--in-container]` and the `AUTHORED_ISOLATION_UNPROVEN` gate, switch-off entry refusal, authored candidates for SP2c claims, the authored path in `ActiveAiDeployment` | 5, 6, 7.3, 7.9, 8, 10 (authority, keys, config, cap, staged deploy), 14 | 1; 2 for Task 10 |
| 4 | `2026-10-11-ai-paper-sp3-04-research-strategy-callers.md` | Research: evaluation of an authored key through the research socket (proxy strategy, one conid per session, used one at a time) on the most recent `research_service.authored_evaluation_sessions` (302) sessions, smoke stage, availability retries, `strategy_trials` on the case, attestation and shadow replay through the sandbox. Strategy service: one live session per deployment per day for all its conids, the live proxy, isolation and digest checks, retire on `SANDBOX_ERROR`, alarm on availability errors | 7.6, 7.7, 8, 10 (pools, lookahead, equivalence, trial count), 14 | 1, 2, 3 |
| 5 | `2026-10-11-ai-paper-sp3-05-author-scoreboard-acceptance.md` | `ai` service: optional `author` role, `authoring:` config, the nightly author loop and its attempts table, authored candidates in the research cycle and the isolation wait; scoreboard view "AI-written vs catalogue" (risk-adjusted); docs and runbooks; end-to-end acceptance over signed RPC | 6, 7.4, 7.8, 11, 10 (config) | 1, 3, 4 (2 for the acceptance test) |

Plans 2 and 3 can run in parallel after Plan 1 (Plan 3 Task 10 waits for Plan 2). Each plan is one PR.

**Who owns a name (rule R0).** The plan that builds a module or the server side of a
method owns its names and wire shape; the others use them exactly as written here.

- **Plan 1:** package `trader/authored/`.
  - `api.py`: `AuthoredStrategy` (class attribute `LOOKBACK_BARS: int = 200`, method
    `on_bar(self, history: pd.DataFrame, params: Mapping[str, float]) -> Decision | None`),
    `Decision` (frozen dataclass: `action: Literal["BUY", "SELL"]`, `probability: float`).
  - `keys.py`: `source_digest(source: bytes) -> str` (returns `"sha256:<hex>"`),
    `authored_key(digest: str, class_name: str) -> str` (returns
    `"authored:sha256:<hex>:<Class>"`), `parse_authored_key(key: str) -> tuple[str, str]`
    (returns `(digest, class_name)`, raises `ValueError`), `is_authored_key(key: str) -> bool`.
  - `allowed_api.py`: `ALLOWED_IMPORTS: frozenset[str]`,
    `ALLOWED_MODULE_ATTRIBUTES: Mapping[str, frozenset[str]]`, `DENIED_CHAIN_NAMES: frozenset[str]`.
  - `static_check.py`: `StaticViolation` (frozen dataclass: `rule: str`, `line: int`,
    `detail: str`), `check_source(source: str) -> list[StaticViolation]`,
    `AuthoredStaticRejected(Exception)` (attribute `violations`),
    `require_clean(source: str) -> str` (returns the single `AuthoredStrategy` subclass name).
  - Test fixtures: `tests/authored/fixtures/orb_authored.py` (`OpeningRangeBreakout`, `MIN_BARS = 40`), used by
    Plan 4's equivalence test; `tests/authored/escapes/*.py` and `SANDBOX_HOSTILE` (the nine stems Plan 2's
    `HOSTILE_ESCAPES` names) in `tests/authored/test_escape_corpus.py`.
  - `scripts/sp3_benchmark.py` and its result in the plan (Plan 1 ruling 1).
- **Plan 2:** package `trader/sandbox/` (`protocol.py`, `supervisor.py`, `server.py`, `child.py`, `posture.py`,
  `caps.py`, `isolation_checks.py`, `probes/`), entry point `python -m trader.sandbox_service`,
  `config_defaults/sandbox-seccomp.json`, compose service `sandbox`, volumes
  `mmr_authored_strategies`, `mmr_sandbox_live`, `mmr_sandbox_research`, the baked `/etc/mmr-build-id`.
  - Constants in `trader/sandbox/protocol.py`: `LIVE_SOCKET = "/run/sandbox-live/sandbox.sock"`,
    `RESEARCH_SOCKET = "/run/sandbox-research/sandbox.sock"`, `STORE_DIR = "/store"`, error codes
    `SANDBOX_ERROR`, `SANDBOX_BUSY`, `SANDBOX_DIGEST_MISMATCH`, `SANDBOX_BAD_REQUEST`, `PROTOCOL_VERSION = 1`,
    `MAX_SESSION_CONIDS = 20`, `PROBE_CONID = 1`, `BUILD_ID_RE`.
  - In `trader/sandbox/supervisor.py`: `Supervisor(*, spawners, pool_sizes, store_dir, image_digest, posture)`,
    `LIVE_POOL_SIZE = 3`, `RESEARCH_POOL_SIZE = 4`, `MAX_CONIDS_PER_SESSION = {"live": 20, "research": 1}`.
    `trader/sandbox/server.py`: `async serve(supervisor, sockets, *, bar_timeout_s=2.0)`.
    Test-only `tests/sandbox/fakes.py`: `InProcessSpawner`.
  - Client `trader/authored/sandbox_client.py`: `SandboxClient(socket_path: str)` with
    `ping() -> SandboxInfo` (`image_digest: str`, the build id; `boot_id`, `pool`, `free`, `size`, `posture`),
    `open_session(digest: str, class_name: str, params: Mapping[str, float], bar_size: str,
    conids: Sequence[int]) -> SandboxSession`, `open_probe(kind, name) -> SandboxSession`;
    `SandboxSession.bar(conid: int, t: datetime, open, high, low, close, volume) -> Decision | None`,
    `SandboxSession.conids`, `SandboxSession.close()`; errors `SandboxUnavailable(Exception)` (busy, unreachable,
    timeout on connect), `SandboxFault(Exception)` (attribute `code`: `SANDBOX_ERROR` or
    `SANDBOX_DIGEST_MISMATCH`) and `SandboxProtocolError(Exception)` (`SANDBOX_BAD_REQUEST`).
  - `isolation_checks.py`: `run_isolation_checks(client: SandboxClient) -> IsolationResult`
    (`passed: bool`, `image_digest: str`, `checks: tuple[CheckResult, ...]`; `CheckResult(name, passed, detail)`),
    `HOSTILE_ESCAPES`. `posture.read_posture()` (incl. `root_read_only`, `pids_max`). No command line of its own.
- **Plan 3:** trader side.
  - Principal `ai_author`; methods `submit_authored_strategy` and `get_authored_strategies`
    (`ai_author`), `list_authored_strategies` (`cli`, `dashboard`), `report_authored_evaluation` (`research`),
    `retire_authored_strategy` (`strategy`), `record_isolation_proof` (`cli`) and `get_isolation_proof`
    (`cli`, `strategy`). Wire shapes: Plan 3 "Cross-plan additions" (tunables `{low, high, default}`).
  - `trader/automation/authored_strategies.py`: `AuthoredState` enum (`STORED`, `EVALUATING`,
    `SHADOW`, `DEPLOYED`, `REJECTED`, `RETIRED`, `EXPIRED`, `FAILED`), `COUNTED_STATES`,
    `AuthoredStrategyStore`, `DeploymentLines`, `withdraw_with`, `authored_retire_command_id`.
  - `trader/research/strategy_key.py`: `AUTHORED_PATH`, `is_authored_path`, `authored_strategy_path`,
    `authored_digest_of_path`, `is_evaluation_key`, `split_evaluation_key`.
    `trader/messaging/ai_deployment_wire.py`: `ACTIVE_STRATEGY_PATH`.
  - Config `ai_paper.authored_strategies` (`enabled`, `max_active` 1-3, `bar_sizes`).
  - Refusal codes: `AUTHORED_STRATEGIES_DISABLED`, `AUTHORED_CAP_FULL`,
    `AUTHORED_STATIC_REJECTED`, `AUTHORED_BAD_TUNABLES`, `AUTHORED_BAD_BAR_SIZE`,
    `AUTHORED_ISOLATION_UNPROVEN`, `AUTHORED_STATE_CONFLICT`, `AUTHORED_STRATEGY_UNKNOWN`.
  - CLI `mmr ai-author prove-isolation [--compose TEXT] [--in-container]` and `mmr ai-author list`;
    `trader/authored/prove_isolation.py`, `trader/authored/isolation_host.py`.
- **Plan 4:** no new RPC method.
  - `trader/research/authored_backtest.py` (the research proxy `AuthoredProxyStrategy`, `connect_sandbox`,
    `RESEARCH_SESSION_CONID`).
  - `trader/research/authored_evaluation.py` (`AuthoredEvaluator`: `STARTED`, smoke stage) and
    `trader/research/authored_outcome.py` (the end and its report bodies).
  - `trader/authored/streaming.py` (`BarStream(session, conid)`, `decision_signal`).
  - `trader/strategy/authored_runtime.py` (live proxy `AuthoredLiveStrategy`, loader `AuthoredDeployments`).
  - `EvaluationPaths.sandbox_socket` (default `RESEARCH_SOCKET`).
  - `ResearchServiceConfig.authored_evaluation_sessions` (default 302) and `authored_walk_forward(config)` in
    `trader/research/service_config.py`.
- **Plan 5:**
  - `trader/ai/author_loop.py`, `author_roles.py`, `author_facts.py`, `author_wire.py`, `author_schema.py`,
    `authored_offers.py`.
  - `ai.yaml` `roles.author` (optional) and `authoring:` (`run_after_close_minutes`,
    `call_timeout_seconds`).
  - `trader/scoreboard/authored_vs_catalogue.py`.

## Migrations

| Database | Plan 1 | Plan 2 | Plan 3 | Plan 4 | Plan 5 |
|----------|--------|--------|--------|--------|--------|
| Trader journal (SP2c uses ≤ 129) | — | — | 130 `authored_strategies`, 131 `authored_isolation_proofs` (132–134 free) | — | — |
| `ai.duckdb` (SP2 1–22, SP2c 30–34) | — | — | — | — | 40 `author_attempts` (41–44 free) |
| Research DB (`trader/research/service_store.py`) | — | — | — | — (the end is kept in the existing `research_requests` columns) | — |

Each plan checks its numbers are free on its base commit before use (Plan 3:
`grep -rn "13[01]" trader/automation trader/data/schema_migrations.py`; Plan 5: `grep -rn "Migration(4[0-4]" trader/ai`).
No legacy data: new tables only; no ALTER on an existing table. The trader's `ai_costs.role` CHECK is not changed:
author spend is booked as `research` there (D12). Migration 40 keeps `author_attempts.tunables_json` as a record of
what was sent; nothing reads it back for evaluation (D5).

## Controller decisions applied at integration (2026-10-11)

Authority order: the spec, then these decisions, then Plan 3 for trader method shapes, Plan 2 for sandbox and
client shapes, Plan 1 for `trader/authored/` names.

- **D1** Live pool: one session per deployment. `OPEN` carries `conids`, `BAR` and `SIGNAL` carry `conid`; one child
  holds one instance per conid. Live pool 3, research pool 4 (one conid each). Plans 2 (ruling 26), 4 (ruling 14).
- **D2** Image identity is Plan 2's baked build id (`/etc/mmr-build-id`, in `PONG`). No `docker inspect`; the posture
  in `PONG` covers read-only root and the pids limit (Plan 2 ruling 27). One spelling (`IMAGE_ID` = `BUILD_ID_RE`).
- **D3** `prove-isolation`: the host runs `compose exec -T research mmr ai-author prove-isolation --in-container`;
  that step prints the proof JSON with no key; the host checks it and records it as `cli` (Plan 3 ruling 15).
- **D4** Plan 3 owns the trader wire shapes; Plans 4 and 5 use them. Plan 3 widens `ActiveAiDeployment` (moved from
  Plan 4 Task 1) and `retire_authored_strategy` also withdraws the version through the SP2c path (Plan 3 ruling 18).
- **D5** Tunables `{low, high, default}`; the default equals the class constant; the record stores it; Plan 5 offers
  cohort point 0 from the record.
- **D6** The sandbox accepts any finite param value; the trader's claim checks cohort points against the ranges.
- **D7** Plan 5's acceptance world sets `EvaluationPaths.sandbox_socket`, not options.
- **D8** Plan 1's ORB fixture has `MIN_BARS = 40`; Plan 4 uses it (Plan 4 ruling 24 dropped). Checked at
  integration: equal to the catalogue ORB bar for bar on Plan 4's 1170 synthetic bars.
- **D9** Plan 1 ships every file Plan 2's `HOSTILE_ESCAPES` names; its corpus test asserts each is refused.
- **D10** Plan 4's proxy module is `trader/research/authored_backtest.py` (listed above).
- **D11** Plan 1 builds the API and the ORB fixture before the benchmark (Plan 1 ruling 2); the table says so.
- **D12** Author spend is booked under role `research` in the trader's `ai_costs` (Plan 5 ruling 14).
- **D13** Spec errata: section 14 "Amendments" plus in-place fixes in 3, 7.5, 7.6, 7.9, 9, 10 and 13.

Extra controller decisions (made while applying the above; the safer option each time):

- **C1** D2 also said not to key the trader's gate on research's `STARTED` report. The trader has no other view of
  the running sandbox, and spec 10 "staged deploy" needs a rebuild to block registration, so the `STARTED` channel
  stays (Plan 3 ruling 12). The strategy service's own `PONG` check before load is the final gate.
- **C2** `max_active` is 1-3, not 1-10 (Plan 3 ruling 4): with one live session per deployment and a live pool of 3,
  a larger cap could deploy a strategy that never gets a session. A test pins it to `LIVE_POOL_SIZE`.
- **C3** The hostile set gains `escape_open_socket` (spec 10 lists "open a socket"; the drafted set had none), and
  `escape_kill_other_child` runs while a benign session is held open, so the check proves the neighbour survives.
- **C4** The backtester's `precompute` does not name its conid, so a research session uses the fixed label
  `RESEARCH_SESSION_CONID = 1`; each conid still gets its own session (Plan 4 ruling 1).

## Owner rulings to confirm

Security:

- Plan 2 ruling 1 / D2 (spec 14): the image identity is a build id baked at `docker.sh -b`; a plain `docker build` leaves it `unset` and the sandbox refuses to start.
- Plan 2 ruling 2 (spec 14): `CAP_CHOWN` at startup only, to give each slot its own 0700 directory, then dropped; fallback without it is no writable directory.
- Plan 2 ruling 3 (spec 14): `RLIMIT_AS` = address space at fork + 256 MiB (an absolute 256 MB fails every pandas child).
- Plan 2 ruling 4: `RLIMIT_CPU` 4 h per session as a backstop; the 2 s wall per bar is the real limit.
- Plan 2 ruling 11 (spec 14): `OPEN_PROBE` is the only path that skips the static check, and only for files in the read-only image.
- Plan 2 ruling 21: known limit, not fixed: children share one network namespace (abstract Unix sockets); the static checker is the layer that keeps authored code away from socket APIs.
- Plan 2 ruling 23: `noexec` on `/tmp` relies on the compose short tmpfs syntax; if a runtime ignores it, `noexec` rests on non-root, no-new-privileges and seccomp.
- Plan 2 ruling 24: the fork-with-threads hazard in the asyncio supervisor is accepted as low risk.
- Plan 2 ruling 26 / D1 (spec 14): one live child serves all conids of one deployment; a fault on any conid ends the whole session.
- Plan 2 ruling 27 / D2 / C3: no `docker inspect`; the proof checks posture incl. read-only root and pids 64; nine hostile files incl. open-socket; kill-other-child with a live neighbour.
- Plan 1 rulings 5-8: the checker refuses `from numpy import x`, extends the deny-list, refuses a bare module alias, and checks every string literal.
- Plan 3 ruling 2: an authored case must hash to the key's digest (`CASE_MALFORMED` otherwise).
- Plan 3 ruling 12 / C1: the registration gate uses the build id from research's latest `STARTED`; the strategy load check on its own `PONG` is the final gate.
- Plan 3 ruling 15 / D3: `prove-isolation` runs inside `research` (research pool, no key) and the host records the result as `cli`.
- Plan 3 ruling 21: isolation records are insert-only and not sealed (no secret, `cli` is the only writer).
- Plan 4 ruling 23: `attest_from_judgment` for an authored key checks the path's digest against the case instead of reading a file.

Money and risk:

- Plan 3 ruling 4 / C2: `max_active` is 1-3; `enabled: true` needs `ai_paper.enabled: true`.
- Plan 3 ruling 16: switch off refuses authored entries at admission and at dispatch; closes always pass.
- Plan 3 ruling 18 / D4: a live fault retires the record and withdraws the deployment version; open positions keep their stops.
- Plan 4 ruling 15: an authored signal carries `risk = 0.0`; Jev and the risk gate size the trade.
- Plan 4 ruling 21: an authored line is never renewed; it ends `EXPIRED`.
- Plan 4 ruling 12: Jev's `strategy_trials` for an authored key = authored files judged so far + this file's trials.
- Plan 5 ruling 8: a registration refused `AUTHORED_ISOLATION_UNPROVEN` waits for the next research evening.
- Plan 5 ruling 14 / D12: author spend is booked as `research` in the trader's `ai_costs`; `ai.duckdb` keeps `author`. Or give Plan 3 trader migration 132 for an `author` role.

Deploy and runbook:

- Plan 3 ruling 23: run `./docker.sh -k` before deploying Plan 3 (new `ai_author` key pair).
- Plan 3 ruling 20: the trader mounts `mmr_authored_strategies` read-write at `/store`; the sandbox mounts it read-only.
- Plan 2 ruling 17: `docker.sh` runs compose from the repo root so the relative seccomp path resolves.
- Plan 2 ruling 18: `sandbox` has compose profiles `ai` and `test`.
- Plan 5 ruling 22: `AiConfig.digest()` gains `authoring`; replays of older units report `config_mismatch`.
- Plan 5 runbook: turn-on order is `docs/SP2_PAPER_DEPLOY.md` section 10 (prove isolation, set the author model, then the switch).

Spec amendments (section 14): live pool per deployment (D1), build id (D2), isolation run in `research` with
`OPEN_PROBE` (D3), startup `CAP_CHOWN`, `RLIMIT_AS` = baseline + 256 MiB, tunables with a default (D5),
`max_active` 1-3 (C2), authored evaluation length 302 sessions (owner confirmed).

Benchmark:

- Plan 1 ruling 1 (spec 14): owner confirmed 302 (2026-10-11). A full authored evaluation was estimated at about 11 h, so authored keys use the most recent 302 sessions (`research_service.authored_evaluation_sessions: 302`, SP2c's folds and embargo, holdout scaled to 39); catalogue strategies keep the full SP2c length. Built by Plan 4 Task 12 (Plan 4 ruling 25). Plan 1 Task 7 still measures and records the hours.

All other rulings:

- Plan 1, all other rulings: see the plan's Rulings section.
- Plan 2, all other rulings: see the plan's Rulings section.
- Plan 3, all other rulings: see the plan's Rulings section.
- Plan 4, all other rulings: see the plan's Rulings section (R1-R6 in its "Cross-plan requirements" are settled).
- Plan 5, all other rulings: see the plan's Rulings section.

## Tests

- Per task: targeted pytest. Full suite once per plan in its last task, under the shared
  lock (`/private/tmp/mmr-suite.lock`), `-n 8 --timeout=120`.
- No real model, IB or network calls. Container tests (Plan 2 Task 11, Plan 3's
  `tests/fullstack/test_prove_isolation_in_research.py`) run in the `fullstack-tests` compose profile against the
  real `sandbox` stanza (`scripts/run_sandbox_fullstack.sh`), never against the live stack.
