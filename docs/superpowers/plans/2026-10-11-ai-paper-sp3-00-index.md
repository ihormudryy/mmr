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
  - `allowed_api.py` (the checker's data; Plan 2's child guard is built from it, Plan 1 ruling 27):
    `ALLOWED_IMPORTS: frozenset[str]` (`numpy`, `pandas`, `math`, `statistics`, `trader.authored.api`;
    `typing` and `dataclasses` are from-import only, Plan 1 ruling 24),
    `ALLOWED_FROM_IMPORTS: frozenset[str]`, `ALLOWED_FROM_IMPORT_NAMES: Mapping[str, frozenset[str]]`,
    `ALLOWED_MODULE_ATTRIBUTES: Mapping[str, frozenset[str]]`, `ALLOWED_BUILTINS: frozenset[str]`,
    `ALLOWED_AST_NODES: frozenset[str]`, `DENIED_CHAIN_NAMES: frozenset[str]`,
    `DENIED_CHAIN_PREFIXES: tuple[str, ...]`, `BANNED_NAMES`, `BANNED_ATTRIBUTES`, `IO_NAMES`,
    `IO_EXEMPT_NAMES`, `PROTECTED_NAMES` (each `frozenset[str]`), `IO_ATTRIBUTE_PREFIXES: tuple[str, ...]`.
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
    `SANDBOX_ERROR`, `SANDBOX_BUSY`, `SANDBOX_UNAVAILABLE` (Plan 2 ruling 34), `SANDBOX_DIGEST_MISMATCH`,
    `SANDBOX_BAD_REQUEST`, `PROTOCOL_VERSION = 1`,
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
    timeout on connect, `SANDBOX_UNAVAILABLE`), `SandboxFault(Exception)` (attribute `code`: `SANDBOX_ERROR` or
    `SANDBOX_DIGEST_MISMATCH`) and `SandboxProtocolError(Exception)` (`SANDBOX_BAD_REQUEST`).
  - `isolation_checks.py`: `run_isolation_checks(client: SandboxClient) -> IsolationResult`
    (`passed: bool`, `image_digest: str`, `checks: tuple[CheckResult, ...]`; `CheckResult(name, passed, detail)`),
    `hostile_check(client, name)`, `HOSTILE_ESCAPES`, `EXPECTED_FAULT_MARKERS`. `posture.read_posture()` (incl.
    `root_read_only`, `pids_max`). No command line of its own.
  - `OPENED` carries `image_digest` (`SandboxSession.image_digest`); `PONG` carries `live_open`
    (`SandboxInfo.live_open`). `supervisor.wipe_slot_as_owner(slot)` empties a slot as its uid.
  - `scripts/run_sandbox_fullstack.sh` uses compose project `mmr-sbxtest` only.
- **Plan 3:** trader side.
  - Principal `ai_author`; methods `submit_authored_strategy` and `get_authored_strategies`
    (`ai_author`), `list_authored_strategies` (`cli`, `dashboard`), `report_authored_evaluation` (`research`),
    `retire_authored_strategy` (`strategy`), `record_isolation_proof` (`cli`) and `get_isolation_proof`
    (`cli`, `strategy`). Wire shapes: Plan 3 "Cross-plan additions" (tunables `{low, high, default}`).
  - `trader/automation/authored_strategies.py`: `AuthoredState` enum (`STORED`, `EVALUATING`,
    `SHADOW`, `DEPLOYED`, `REJECTED`, `RETIRED`, `EXPIRED`, `FAILED`), `COUNTED_STATES`,
    `AuthoredStrategyStore` (incl. `send_gate_in_tx`, the SUBMITTING-transaction check, and
    `require_proven_in_tx`), `DeploymentLines`, `withdraw_with`, `authored_retire_command_id`.
  - `trader/research/strategy_key.py`: `AUTHORED_PATH`, `is_authored_path`, `authored_strategy_path`,
    `authored_digest_of_path`, `is_evaluation_key`, `split_evaluation_key`.
    `trader/messaging/ai_deployment_wire.py`: `ACTIVE_STRATEGY_PATH`.
  - Config `ai_paper.authored_strategies` (`enabled`, `max_active` 1-3, `bar_sizes`).
  - Refusal codes: `AUTHORED_STRATEGIES_DISABLED`, `AUTHORED_CAP_FULL`,
    `AUTHORED_STATIC_REJECTED`, `AUTHORED_BAD_TUNABLES`, `AUTHORED_BAD_BAR_SIZE`,
    `AUTHORED_ISOLATION_UNPROVEN`, `AUTHORED_STATE_CONFLICT`, `AUTHORED_STRATEGY_UNKNOWN`.
  - CLI `mmr ai-author prove-isolation [--compose TEXT] [--in-container]` (refuses `LIVE_SESSIONS_OPEN` while a live
    sandbox session is open) and `mmr ai-author list`;
    `trader/authored/prove_isolation.py`, `trader/authored/isolation_host.py`.
- **Plan 4:** no new RPC method.
  - `trader/research/authored_backtest.py` (the research proxy `AuthoredProxyStrategy`, `connect_sandbox`,
    `RESEARCH_SESSION_CONID`).
  - `trader/research/authored_evaluation.py` (`AuthoredEvaluator`: `STARTED`, smoke stage) and
    `trader/research/authored_outcome.py` (the end and its report bodies).
  - `trader/authored/streaming.py` (`BarStream(session, conid)`, `decision_signal`).
  - `trader/strategy/authored_runtime.py` (live proxy `AuthoredLiveStrategy(..., proof_gate)`, loader
    `AuthoredDeployments` with `is_proven`, which reads the trader proof fresh on every call).
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
| Trader journal (SP2c uses ≤ 129) | — | — | 130 `authored_strategies`, 131 `authored_isolation_proofs` | — | 132 `sp3_ai_costs_author_role` (`ai_costs.role` gains `author` by a table rebuild that keeps every existing row; 133–134 free) |
| `ai.duckdb` (SP2 1–22, SP2c 30–34) | — | — | — | — | 40 `author_attempts` (41–44 free) |
| Research DB (`trader/research/service_store.py`) | — | — | — | — (the end is kept in the existing `research_requests` columns) | — |

Each plan checks its numbers are free on its base commit before use (Plan 3:
`grep -rn "13[01]" trader/automation trader/data/schema_migrations.py`; Plan 5: `grep -rn "Migration(4[0-4]" trader/ai`).
No legacy data: new tables only; no ALTER on an existing table, with one owner-approved exception: migration 132
rebuilds `ai_costs` with a role CHECK that also accepts `author` (owner ruling 2026-10-11, Plan 5 ruling 14). The SP2
paper soak will have written cost rows by then, so the rebuild follows journal migration 52's pattern (create the
wider table, `INSERT ... SELECT *` every row, drop, rename) inside the one transaction `SchemaMigrator.apply` opens:
rows keep identical values and their seals stay valid, and a failure halfway rolls everything back. Plan 5 Task 2
tests both. Migration 40 keeps `author_attempts.tunables_json` as a record of
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
- **D12** (superseded by the owner, 2026-10-11) Author spend has its own role `author` in the trader's `ai_costs`
  (trader migration 132, Plan 5 Task 2 and ruling 14).
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
- Plan 2 ruling 2 (spec 14; owner confirmed 2026-10-12): `CAP_CHOWN` at startup only, to give each slot its own 0700 directory, then dropped; fallback without it is no writable directory.
- Plan 2 ruling 3 (spec 14): `RLIMIT_AS` = address space at fork + 256 MiB (an absolute 256 MB fails every pandas child).
- Plan 2 ruling 4: `RLIMIT_CPU` 4 h per session as a backstop; the 2 s wall per bar is the real limit.
- Plan 2 ruling 11 (spec 14): `OPEN_PROBE` is the only path that skips the static check, and only for files in the read-only image.
- Plan 2 ruling 21: known limit, not fixed: children share one network namespace (abstract Unix sockets); the static checker is the layer that keeps authored code away from socket APIs.
- Plan 2 ruling 23: `noexec` on `/tmp` relies on the compose short tmpfs syntax; if a runtime ignores it, `noexec` rests on non-root, no-new-privileges and seccomp.
- Plan 2 ruling 24: the fork-with-threads hazard in the asyncio supervisor is accepted as low risk.
- Plan 2 ruling 26 / D1 (spec 14): one live child serves all conids of one deployment; a fault on any conid ends the whole session.
- Plan 2 ruling 27 / D2 / C3 (review r1 #3): no `docker inspect`; the proof checks posture incl. read-only root and pids 64; nine hostile files incl. open-socket; a hostile check passes only if the escape OPENED, faulted at BAR with its expected marker, and a neighbouring benign session and PING survive; the proof also runs the probes for the custom seccomp profile (ptrace), the child's slot uid and caps, its descriptors and the slot wipe in a real child (final review, 2026-10-12).
- Plan 2 ruling 28 (review r1 #2): a slot is wiped by a helper running as the slot uid, so the serving supervisor keeps only SETUID, SETGID and KILL; a failed wipe quarantines the slot.
- Plan 2 ruling 29 / owner ruling 2026-10-11: `OPENED` reports the session's build; `PONG` reports `live_open`; `prove-isolation` refuses (`LIVE_SESSIONS_OPEN`) while any live session is open; both ways under the slot lock: no probe starts beside a live session, and no live session starts while a probe is reserved, open or not yet wiped (`SANDBOX_BUSY`, `isolation probe running`; PR #159 review).
- Plan 2 ruling 30 (review r1 #8, r2 #2): the full-stack runner starts only `sandbox` and `fullstack-tests` (never ib-gateway), under `env -i` (no operator broker/provider variables), with a test-only image tag (never `mmr:latest`), project `mmr-sbxtest` and its own volumes, no published host port, a throwaway `HOME` with copied config and test-only keys, no `.env`. The render guard inspects every service's environment and image and refuses before `up` on any ib-gateway service, credential variable with a value, production image tag, published port, live-config bind or `.env`.
- Plan 2 ruling 5: other child rlimits: `RLIMIT_NPROC` 1 (no thread, no fork), `RLIMIT_FSIZE` 4 MiB, `RLIMIT_CORE` 0; the BLAS thread variables are set to 1 in code before numpy loads.
- Plan 2 ruling 6: the 2 s step limit covers the class load at `OPEN` as well as each `BAR`.
- Plan 2 ruling 7: one session per connection; the child is killed when the caller's connection closes, so a crashed caller cannot leak a slot.
- Plan 2 ruling 8: new code `SANDBOX_BAD_REQUEST` for caller mistakes (client: `SandboxProtocolError`); it is neither availability nor a strategy fault.
- Plan 2 ruling 9: a missing store file, one that is not a regular file, bytes that do not match or a class that differs from the file's class is `SANDBOX_DIGEST_MISMATCH` (a store file the sandbox may not read is `SANDBOX_UNAVAILABLE`, ruling 34); a static rejection at `OPEN` is `SANDBOX_ERROR` with `AUTHORED_STATIC_REJECTED:<rule>`.
- Plan 2 ruling 10: `PONG` carries `image_digest`, `boot_id`, `pid`, `pool`, `free`, `size`, `live_open` and `posture`; `SandboxInfo` carries the same fields except `pid` (additive to the index).
- Plan 2 ruling 34 (final review, controller ruling 2026-10-12, spec 8): a sandbox-side failure (fork or socketpair, the child's privilege drop, rlimits or setup, a store file it may not read, an internal error) is the new code `SANDBOX_UNAVAILABLE`, which the client raises as `SandboxUnavailable`; only a fault the strategy caused stays `SANDBOX_ERROR` or `SANDBOX_DIGEST_MISMATCH`. A child's setup failure counts only in place of its first frame (`SETUP_OK`, sent before any authored code loads), so authored code cannot forge one.
- Plan 2 ruling 12: the fake child is a `Supervisor` test argument only; the service always uses `ForkSpawner` and no option, env var or argument can change it; a test pins that no `trader/` module imports `tests`.
- Plan 2 ruling 13: a failed tmpfs wipe quarantines the slot (never reused, logged at ERROR, `PONG.free` drops).
- Plan 2 ruling 14: `init: true` and `ipc: none`; `entrypoint` replaces `docker-entrypoint.sh`.
- Plan 2 ruling 15: the supervisor joins group `trader`, sets the socket directories to root:trader 0750 and the sockets to 0660; a child calls `setgroups([])` before dropping.
- Plan 2 ruling 16: seccomp = moby v27.5.1 `default.json` (sha256 `9c1025c88ccaa517b648da571961838744ea2137f176bfe6a48b21294cae9c76`) minus `ptrace`, `process_vm_readv`, `process_vm_writev`, `kcmp`, `pidfd_open`; default action `SCMP_ACT_ERRNO`.
- Plan 2 ruling 19: `sandbox` holds no key: it is skipped in `test_every_long_lived_service_is_classified`, and a static test asserts it mounts no key and no config.
- Plan 2 ruling 20: bar times on the wire are UTC ISO-8601; the child's `history` index is tz-aware UTC named `date`.
- Plan 2 ruling 25: every request except `PING` runs in a worker thread, the supervisor lock covers only O(1) bookkeeping, and a second bar in flight on one session is refused.
- Plan 2 ruling 31: the child's import guard and built-ins come from Plan 1's `allowed_api`, never from wider lists.
- Plan 2 ruling 32: `OPEN_PROBE` is accepted only on the research pool, so a hostile probe never takes a live slot.
- Plan 2 ruling 33: `network_none` passes on `lo` plus only Docker Desktop's fallback tunnel devices, and only while down and without an address.
- Plan 1 rulings 5-8: the checker refuses `from numpy import x`, extends the deny-list, refuses a bare module alias, and checks every string literal.
- Plan 1 rulings 11-27: the stricter checker found in review (from-import only `typing`/`dataclasses`, builtin and AST-node allow-lists, chain prefixes, literal module-level values, no `match`) and Plan 2's child guard built from `allowed_api`.
- Plan 3 ruling 2: an authored case must hash to the key's digest (`CASE_MALFORMED` otherwise).
- Plan 3 ruling 12 / C1 (review r1 #5): registration needs a passing proof for both the build that evaluated the record and the build research reported last, rechecked inside the seal transaction; it does not prove the build running now. The binding gate is the strategy service's: no live session on a build without a passing proof (Plan 4 ruling 19).
- Plan 3 ruling 15 / D3: `prove-isolation` runs inside `research` (research pool, no key) and the host records the result as `cli`.
- Plan 3 ruling 21: isolation records are insert-only and not sealed (no secret, `cli` is the only writer).
- Plan 4 ruling 23: `attest_from_judgment` for an authored key checks the path's digest against the case instead of reading a file.

Money and risk:

- Plan 3 ruling 4 / C2: `max_active` is 1-3; `enabled: true` needs `ai_paper.enabled: true`.
- Plan 3 ruling 16 / 18 (review r1 #4, r2 #3): switch off or retirement refuses authored entries at admission, at dispatch and on the saga's SUBMITTING transaction (`send_gate_in_tx`), which resolves the record from the entering version's base deployment so a renewed version is fenced too; closes always pass.
- Plan 3 ruling 18 / D4: a live fault retires the record and withdraws the deployment version; open positions keep their stops.
- Plan 4 ruling 15: an authored signal carries `risk = 0.0`; Jev and the risk gate size the trade.
- Plan 4 ruling 21: an authored line is never renewed; it ends `EXPIRED`.
- Plan 4 ruling 12: Jev's `strategy_trials` for an authored key = authored files judged so far + this file's trials.
- Plan 5 ruling 8: a registration refused `AUTHORED_ISOLATION_UNPROVEN` waits for the next research evening.
- Plan 5 ruling 14 (owner confirmed 2026-10-11): author spend has its own `ai_costs` role `author` (trader migration 132 rebuilds the table, rows and seals copied unchanged).
- Plan 4 ruling 19 (review r1 #6, r2 #4): a passing proof is never cached; `is_proven` reads the authoritative trader proof for the build on every call, before every new session and on every bar, fail-closed, with a 2 s timeout on its own query client. Built 2026-10-12: after a read that got no answer (trader down, timeout, remote error) the gate stays shut for 20 s without asking, because the read then ran on the strategy service's event loop; a definite answer is never held. Final review I2: an authored instance's bars now run on its own worker thread, so only the dispatch check of a signal reads the proof on the loop.
- Plan 4 ruling 10 (review r1 #7): an authored end the trader refuses for good parks the request (claim open, `AUTHORED_REPORT_REFUSED`, resent hourly and at restart); refusal is never delivery.

Deploy and runbook:

- Plan 3 ruling 23: run `./docker.sh -k` before deploying Plan 3 (new `ai_author` key pair).
- Plan 3 ruling 20: the trader mounts `mmr_authored_strategies` read-write at `/store`; the sandbox mounts it read-only.
- Plan 2 ruling 17: `docker.sh` runs compose from the repo root so the relative seccomp path resolves.
- Plan 2 ruling 18: `sandbox` has compose profiles `ai` and `test`.
- Plan 2 ruling 22: the Dockerfile creates `/store` owned by `trader` (0755); Plan 3 mounts `mmr_authored_strategies` there read-write and writes files with mode 0644.
- Plan 5 ruling 22: `AiConfig.digest()` gains `authoring`; replays of older units report `config_mismatch`.
- Plan 5 runbook: turn-on order is `docs/SP2_PAPER_DEPLOY.md` section 10 (prove isolation, set the author model, then the switch); a later prove-isolation run needs the switch off and the live sessions closed first.

Spec amendments (section 14): live pool per deployment (D1), build id in `PONG` and `OPENED` (D2), isolation run in
`research` with `OPEN_PROBE` and only with no live session open (D3, owner), startup-only `CAP_CHOWN` and the slot
wipe as the slot uid, `RLIMIT_AS` = baseline + 256 MiB, tunables with a default (D5), `max_active` 1-3 (C2),
registration evidence vs the binding live gate (C1, review r1 #5), authored evaluation length 302 sessions (owner
confirmed).

Benchmark:

- Plan 1 ruling 1 (spec 14): owner confirmed 302 (2026-10-11). Measured 2026-10-12: a full authored evaluation (10 conids x 690 sessions x 3 cohort points, 2 tunables, 2 neighbours each, 6 folds, embargo 5, + 90-session holdout) is estimated at 10.57 h at an upper-bound combined cost of 0.8872 ms/bar (streamed + in-process backtest, strategy time counted twice). Since 10.57 h is over 4 h: reduce research_service.period_sessions for authored keys only to N=310 (>= 132: 6 folds, embargo 5, holdout 90); never weaken the sandbox. Owner kept 302 (not the script's 310): authored keys use the most recent 302 sessions (`research_service.authored_evaluation_sessions: 302`, SP2c's folds and embargo, holdout scaled to 39); catalogue strategies keep the full SP2c length. At the selected configuration (302 sessions, holdout scaled to 39; otherwise as above) the same upper bound of 0.8872 ms/bar gives about 4.6 h (4.64 h, 18,813,600 bar sends; `selected_configuration_text` in `scripts/sp3_benchmark.py`, same arithmetic as `extrapolate`). That is above the 4 h planning line; **the owner accepted it (2026-10-12)**, as it fits the ~15 h overnight window. Built by Plan 4 Task 12 (Plan 4 ruling 25).

All other rulings:

- Plan 1, all other rulings: see the plan's Rulings section.
- Plan 2, all other rulings: see the plan's Rulings section.
- Plan 3, all other rulings: see the plan's Rulings section.
- Plan 3, made during the build (2026-10-12):
  - Retirement, refresh and proof writes take the trader journal's write lock, the same lock as the saga's SUBMITTING write, so a retirement and a send (or a failed proof and the seal) never commit without seeing each other under DuckDB snapshot isolation.
  - A retirement whose withdrawal fails for a reason other than an SP2c refusal replies `REFUSED AUTHORED_WITHDRAWAL_FAILED` with `retryable: true` and the record already `RETIRED`; `refresh()` withdraws again; Plan 4's resend loop caps its resends.
  - `tests/fullstack/test_prove_isolation_in_research.py` is written but not run: Plan 2's runner starts no `research` service. Plan 5's acceptance runs it.
- Plan 4, all other rulings: see the plan's Rulings section (R1-R6 in its "Cross-plan requirements" are settled).
- Plan 4, made during the build (2026-10-12):
  - A bar Plan 2's protocol refuses (high < low, negative volume, time outside 1970-2100, non-finite) is skipped; a live bar dated after today (New York) is skipped and never moves the session day.
  - A research or live session on a build other than the STARTED build, and a fault at OPEN after the sandbox moved to another build, count as `SANDBOX_UNAVAILABLE`, never as the strategy's fault.
  - The trader's claim also refuses (`STRATEGY_NOT_ALLOWED`) a cohort whose points do not name exactly the stored tunables or whose point 0 is not the stored defaults; Plan 5's author must build cohorts that way.
  - A parked authored request (a refused STARTED, a wire error) closes its SP2c claim FAILED, resent each tick, so Plan 3's 24 h grace returns the record to `STORED`.
  - The strategy service resends a retryable retire refusal or a remote error at most 20 times per digest, then logs one ERROR; an unreachable trader and `AUTHORED_STORE_UNAVAILABLE` are resent without counting. Pending retirements live in memory (ruling 18).
  - A crashing authored instance is re-enabled after 60 s, doubling to at most 30 min; a decided bar resets it.
  - The end report uses `update_claim`'s loud-code rule: a non-loud remote code (`AUTHORED_STORE_UNAVAILABLE`, `PERMISSION_DENIED`, `AUTHENTICATION_ERROR`) stops the research service; a restart resends.
- Plan 5, all other rulings: see the plan's Rulings section.

## Tests

- Per task: targeted pytest. Full suite once per plan in its last task, under the shared
  lock (`/private/tmp/mmr-suite.lock`), `-n 8 --timeout=120`.
- No real model, IB or network calls. Plan 2's container tests run in the `fullstack-tests` compose profile against
  the real `sandbox` stanza (`scripts/run_sandbox_fullstack.sh`), never against the live stack. Plan 3's
  `tests/fullstack/test_prove_isolation_in_research.py` needs a `research` service that runner does not start; Plan 5's
  acceptance runs it.
