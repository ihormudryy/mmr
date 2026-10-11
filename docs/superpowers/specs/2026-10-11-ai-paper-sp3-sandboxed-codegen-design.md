# AI Paper Bot — SP3: AI-Written Strategies in a Sandbox — Design

Status: design approved section by section by the owner on 2026-10-11; this file is
the written spec for owner review. Revised after review rounds 1 and 2 on PR #149 (Grok, OpenAI).
Tickets: #37 (epic), #109 (this spec), #110 (plans). Builds on SP1
(`2026-10-05-ai-paper-sp1-foundation-design.md`), SP2a+b
(`2026-10-07-ai-paper-sp2ab-autonomous-loop-design.md`) and SP2c
(`2026-10-08-ai-paper-sp2c-backtest-judge-design.md`), all merged.

## 1. Goal

An AI model writes new strategy code. The code runs only in a locked-down sandbox
that can compute signals and nothing else. A written strategy trades on paper only
after the full SP2c gate (evaluation, holdout, Jev DEPLOY, attestation,
registration). SP3 succeeds when an AI-written strategy reaches DEPLOY and beats
the catalogue strategies on the same symbols on a **risk-adjusted** measure (7.8).
The success measure is a scoreboard report; it never loosens the SP2c gate.

## 2. Owner decisions (2026-10-11; reviewers may challenge any of them)

- **What SP3 proves:** AI strategies from the AI's own ideas, compared with the
  catalogue on forward paper results, risk-adjusted (changed after review round 1:
  raw P&L can be won by one lucky name).
- **Author:** a new `author` model role with its own `ai.yaml` entry (any backend).
  Jev stays an independent judge. Spend counts against the same New York day
  budget.
- **Cadence:** at most one new strategy per night, after the close.
- **Cap:** at most 3 AI-written strategies in the counted states (7.3).
- **Authority:** automatic while the owner switch is on; only Jev DEPLOY plus
  attestation lets one trade. No per-strategy owner approval.
- **Staged deploy (added after review round 2, OpenAI):** authoring and evaluation
  may run, but no authored strategy is deployed until the isolation acceptance
  (7.9) has passed on the exact sandbox image that is running.
- **Sandbox:** one locked-down container (approach A). Not a restricted subprocess
  in an existing container, not a container per run.
- **Storage:** a content-addressed store (sha256 digest), no git branch.

Background decision (SP1 spec, section 3): the owner accepted "AI-written code in
V1" as a risk, on condition that the sandbox is correct from the first day.

## 3. Words used in this spec

| Word | Meaning |
|---|---|
| Authored strategy | A strategy file written by the `author` role. |
| Catalogue strategy | A strategy file in the repo's `strategies/`. |
| Store | The read-only, content-addressed volume that holds authored files. |
| Digest | `sha256:` of the file bytes. The store key and the binding key. |
| Session (sandbox) | One child process serving one strategy for one run: live, all conids of one deployment; research, one conid (amended, 14). |
| Authored key | `authored:sha256:<hex>:<Class>`, the strategy key of an authored file. |

## 4. Findings in today's code that shape this design

- A strategy instance gets `self.storage` (the price database) and, live,
  `self.strategy_runtime` (the whole runtime object). Nothing restricts imports;
  the only "sandbox" today is a path check under `strategies/`.
- The backtester handed `on_bar` the full price frame (#150, fixed by PR #151 for
  index reads only), `precompute` state holds the full history by contract, and
  `assert_no_lookahead` covers only `precompute`. Research backtests run in the
  `research` container, which also holds the research signing key.
- The SP2c binding covers the strategy file bytes, class, params, conids and bar
  size. It does not cover helper modules, so an authored file may import none.
- Every service inherits `x-mmr-hardening` (network `mmr-internal`, user
  `trader`, tmpfs for `/tmp`, `/home/trader/.tmp`, `/home/trader/.cache`) and
  `x-mmr-common-env` (provider API keys, IB account). The image has one user,
  `trader` (uid 1000, in group `sudo`), with `HOME=/home/trader` and
  `TMPDIR=$HOME/.tmp`.

So AI code must never run in the `research`, `strategy`, `trader` or `ai`
processes, lookahead must be impossible by construction, and the sandbox service
cannot reuse the shared compose anchors.

## 5. Roles, principals and keys

| Unit / principal | May | May not |
|---|---|---|
| `author` role (in `ai`) | Write one file, its tunables and a note per night | Run code, store files, choose symbols outside the menu, judge |
| `ai_author` principal (new) | `submit_authored_strategy`, `get_authored_strategies` | Every other method |
| `trader` | Static check, write the store, keep the records, offer candidates | Run authored code |
| `research` | Evaluate authored strategies through the research socket | Run authored code in process |
| `strategy` | Stream live bars through the live socket, record signals | Run authored code in process |
| `sandbox` | Run authored code on bars it was sent | Network, keys, config, databases, writes outside its tmpfs |
| Jev | Judge DEPLOY / SHADOW / REJECT as in SP2c | Judge its own code (it writes none) |

`ai_author` key wiring (same pattern as `ai_research`):

- `keys/rpc/ai_author.key` (0600) and `ai_author.pub`, created by `mmr keys init`
  / `./docker.sh -k`.
- `SERVICE_EXTRA_PRINCIPALS["ai"]` becomes `("ai_research", "ai_author")`; the `ai`
  service mounts `ai_author.key` read-only. The trader mounts `ai_author.pub`.
- No other service mounts `ai_author.key`; `check-mount` fails if one does.
  `RESTART_ON_ROTATE["ai_author"] = ("ai", "trader")`; `KEYCHECK_SERVICES` covers it.
- Allow-list: only `ai_author` may call `submit_authored_strategy` and
  `get_authored_strategies` (a read of its own records and the cap); everyone else
  gets `PERMISSION_DENIED`.

`ai_sandbox` from the SP1 principal table has no key and no methods; the sandbox
speaks no typed RPC.

## 6. Owner configuration

`trader.yaml`, read only from the file:

```yaml
ai_paper:
  authored_strategies:
    enabled: false   # the owner switch
    max_active: 3    # counted states, 7.3
```

`load_ai_paper_config` accepts this block (today an unknown key) and still refuses
any `AI_PAPER*` environment override. Switch off stops new authoring, new
evaluations of authored strategies, and new entries from deployed authored
strategies (`AUTHORED_STRATEGIES_DISABLED`). Closes always stay allowed. An
authored DEPLOY also counts toward SP2c `max_active_deploys`.

`ai.yaml`:

```yaml
roles:
  author:                       # optional
    backend: openrouter
    model: ""                   # empty = authoring off; the ai service still starts
    max_input_tokens: 60000
    max_output_tokens: 8000
authoring:
  run_after_close_minutes: 60
  call_timeout_seconds: 300     # the author's own limit
```

`ROLE_NAMES` gains `author` as an **optional** role: a missing role or an empty
model disables authoring and is not `ROLE_MODEL_MISSING`. The author call uses
`authoring.call_timeout_seconds`, not the role's `call_timeout_seconds`, so it is
not bounded by `budget.decision_deadline_seconds` and does not raise the
orchestrator/Jev deadline. A price row is required for the author model as for
the others.

## 7. Components

### 7.1 Authored-strategy API

`trader/authored/api.py` (the only repo module an authored file may import):

```python
class AuthoredStrategy:
    LOOKBACK_BARS: int = 200          # 1..2000; bars the strategy may see
    def on_bar(self, history: pd.DataFrame, params: Mapping[str, float]) -> Decision | None: ...

@dataclass(frozen=True)
class Decision:
    action: Literal["BUY", "SELL"]
    probability: float                # 0..1
```

`history` holds the last `LOOKBACK_BARS` completed bars (columns `open, high, low,
close, volume`; DatetimeIndex). Tunables are upper-case class constants; `params`
carries their values. No storage, runtime, logger, clock, randomness or conid
lookup. Single symbol, long only. Jev and the risk gate size the trade, as for
catalogue signals.

### 7.2 Static checker

`trader/authored/static_check.py`, pure code, no execution. Runs at the author,
at the trader (authority) and in the sandbox before load. The checker is a
second layer; the process boundary (7.5) is the isolation. Two allow-lists:

- **Module attributes:** an attribute of an imported module (`np.x`, `pd.x`,
  `math.x`, `statistics.x`) must be on an explicit list in
  `trader/authored/allowed_api.py` (for example `pd.DataFrame`, `pd.Series`,
  `pd.isna`, `pd.Timedelta`, `np.mean`, `np.where`, `np.nan`). Nothing below a
  module attribute may be a module: `pd.io`, `pd.core`, `pd.api`, `pd.compat`,
  `np.lib`, `np.random`, `np.ctypeslib`, `np.testing` and the like are refused.
- **Attribute names anywhere in a chain:** refused if the name is on a deny-list of
  module and process names (`os`, `sys`, `io`, `common`, `core`, `compat`, `api`,
  `lib`, `util`, `testing`, `builtins`, `modules`, `subprocess`, `signal`,
  `kill`, `getppid`, `system`, `popen`, `plot`, `style`, `sql`) in addition to the
  rules below.

A test walks the **shipped** pandas and numpy: for every allowed module attribute
it asserts that the object is not a module and that none of its public attributes
is `os`, `sys`, `subprocess` or `builtins`. Also refused, with
`AUTHORED_STATIC_REJECTED` plus the rule:

- an import other than `numpy`, `pandas`, `math`, `statistics`, `dataclasses`,
  `typing`, `trader.authored.api`;
- any name or attribute starting with `_`;
- the names `eval`, `exec`, `compile`, `open`, `__import__`, `globals`, `locals`,
  `vars`, `getattr`, `setattr`, `delattr`, `input`, `breakpoint`, `memoryview`,
  `format`, and the attributes `format`, `format_map`;
- pandas and numpy I/O and string evaluation: attributes `read_*`, `to_*`
  (except `to_numpy`), `load`, `save`, `savez`, `fromfile`, `tofile`, `eval`,
  `query`, `ctypeslib`, `pickle`;
- module-level statements other than imports, constant assignments and exactly
  one class that subclasses `AuthoredStrategy`;
- `global` / `nonlocal`, `async`, `yield`, decorators, lambdas with defaults,
  `try` / `except`;
- a file over 20 KB, or one that does not parse.

### 7.3 Store, records and the cap (trader)

- Volume `mmr_authored_strategies`. The trader mounts it read-write; `sandbox`
  mounts it read-only; `ai`, `research` and `strategy` do not mount it (they pass
  the digest; only the sandbox loads code).
- File `<hex>.py` plus `<hex>.json` (class, tunables, bar size, author note,
  author model, created time). Temp file and atomic rename, file before record.
- Table `authored_strategies` (trader DB): digest (key), class, state, reason,
  attempts, created, state history.

| State | Counted in `max_active` | Next |
|---|---|---|
| `STORED` | yes | `EVALUATING` |
| `EVALUATING` | yes | `REJECTED`, `SHADOW`, `DEPLOYED`, back to `STORED` (sandbox unreachable), `FAILED` |
| `SHADOW` | yes | `RETIRED`, `EXPIRED` |
| `DEPLOYED` | yes | `RETIRED`, `EXPIRED` |
| `REJECTED`, `RETIRED`, `EXPIRED`, `FAILED` | no (terminal) | — |

Reasons on terminal states include `SANDBOX_ERROR` (code fault),
`SANDBOX_UNAVAILABLE` (3 nights without a reachable sandbox), `JEV_REJECT`,
`OWNER_RETIRED`.

- `submit_authored_strategy` (`ai_author`): checks the switch, the cap, the
  static check, the bar size (from `research.bar_sizes`), the tunables (numbers
  only, each with a range), then writes the store and the record. An existing
  digest returns the existing record (idempotent). Refusals:
  `AUTHORED_STRATEGIES_DISABLED`, `AUTHORED_CAP_FULL`,
  `AUTHORED_STATIC_REJECTED`, `AUTHORED_BAD_TUNABLES`, `AUTHORED_BAD_BAR_SIZE`.
  Registration of an authored DEPLOY is refused `AUTHORED_ISOLATION_UNPROVEN`
  while 7.9 has no passing record for the running image.

### 7.4 Author loop (ai service)

Runs once per New York trading day at close + `run_after_close_minutes` (same New
York day, so the same budget day) when: the author model is set, the switch is
on, the cap has room (`get_authored_strategies`), the experiment is not KILLED,
and the budget has room. Input, built by code: the API text, the research
universe and allowed bar sizes, catalogue evaluation stats and scoreboard
results, every earlier authored strategy with its outcome and Jev's reason, and
the current authored trial count (7.7). Output (schema checked): file text,
tunables with ranges, bar size, a note of at most 600 chars. The author runs the
static check; on a failure it gets the error back and one retry. A second failure
ends the night; the attempt and its reason are recorded in `ai.duckdb`. Then
`submit_authored_strategy`. The `ai` service never reaches the sandbox or the
store.

### 7.5 Sandbox service

The `sandbox` stanza does **not** inherit `x-mmr-hardening` or
`x-mmr-common-env`. It sets:

- `network_mode: none`, `read_only: true`, `cap_drop: [ALL]`,
  `cap_add: [CHOWN, SETUID, SETGID, KILL]` (CHOWN only at startup, then dropped; 14),
  `security_opt: [no-new-privileges:true,
  seccomp=./config_defaults/sandbox-seccomp.json]`, `pids_limit: 64`, `cpus: 2`,
  `mem_limit: 4g`.
- **Process model.** The supervisor runs as uid 0 inside the container and, once
  started, with only SETUID, SETGID and KILL (no other root power: read-only root, no network, no
  mounts of value). Each session's child gets its **own** uid from a pool
  (`20001..20007`, one per session slot, group `sbxchild` gid 20000, none in
  `sudo` or `trader`), set with `setgid`/`setuid` after `fork` and before any
  authored code loads; it keeps no capability. A child therefore cannot signal,
  `ptrace` or read the memory of the supervisor (different uid, no `CAP_KILL`) or
  of another child (different uid). The supervisor uses `CAP_KILL` only to stop
  its own children. The seccomp profile is Docker's default plus a deny of
  `ptrace`, `process_vm_readv`, `process_vm_writev`, `kcmp` and `pidfd_open`.
- The Dockerfile adds group `sbxchild` and the uid range; image files stay owned
  by `trader` and are world-readable only (no child-writable path but its own
  tmpfs directory).
- `environment:` only `TZ=UTC`, `PYTHONDONTWRITEBYTECODE=1`, `HOME=/tmp`,
  `TMPDIR=/tmp`, `NUMBA_CACHE_DIR=/tmp/numba`, `MPLCONFIGDIR=/tmp/mpl`. No provider
  key, IB account or path to any config.
- `tmpfs: /tmp` (64 MB, `noexec`); each child gets `/tmp/s<slot>` owned by its
  uid, mode 0700; the supervisor wipes it when the session ends.
- Volumes: `mmr_authored_strategies` read-only at `/store`, `mmr_sandbox_live` at
  `/run/sandbox-live`, `mmr_sandbox_research` at `/run/sandbox-research`. Nothing
  else: no `~/.config`, keys, `.env`, data or logs volume (it logs to stdout).
- Same image as the other services, so numpy and pandas versions match.
- Health: each socket answers `PING`.

Sockets and pools (separate, so research cannot starve live trading):

| Socket | Mounted by | Pool |
|---|---|---|
| `/run/sandbox-live/sandbox.sock` | `sandbox`, `strategy` | 3 sessions, one per authored deployment, each serving all its conids (amended, 14) |
| `/run/sandbox-research/sandbox.sock` | `sandbox`, `research` | 4 sessions, one conid each |

Each socket file is created with group `trader` (gid 1000, which `strategy` and
`research` run as) and mode `0660`, so those callers connect and no other user
can. The supervisor (uid 0) creates the sockets and sets that group; children
never see the socket (their only descriptor is the pipe to the supervisor). No
other service mounts either volume.

Protocol, frames of 4-byte length plus JSON, `"v": 1`:

| Request | Reply |
|---|---|
| `OPEN {digest, class, params, bar_size, conids}` | `OPENED {session, image_digest}` or `ERROR {code}` |
| `OPEN_PROBE {kind, name, conids}` (isolation checks only, 14) | `OPENED {session, image_digest}` or `ERROR {code}` |
| `BAR {session, conid, t, open, high, low, close, volume}` | `SIGNAL {session, conid, t, action, probability}` (action may be null) |
| `CLOSE {session}` | `CLOSED` |
| `PING` | `PONG {image_digest (the build id), posture, live_open, ...}` |

`OPEN` re-hashes the stored file (`SANDBOX_DIGEST_MISMATCH`), runs the static
check, then forks one child, drops it to its slot uid, and only then loads the
class. A session holds, per conid, only bars it
was sent for that conid, trimmed to `LOOKBACK_BARS`; `BAR` times must strictly
increase per conid. A full
pool answers `SANDBOX_BUSY` at once (no queue). Limits per child: 2 s wall per
bar, `RLIMIT_CPU`, `RLIMIT_AS` = address space at fork + 256 MiB (14), `RLIMIT_NPROC` blocking new processes,
file descriptors closed except the pipe to its parent; import guard and reduced
built-ins inside. Any limit hit, exception or bad output (action not BUY/SELL/null,
probability outside 0..1 or NaN) kills the child and returns
`ERROR {code: SANDBOX_ERROR, detail}`; the service keeps serving other sessions.

### 7.6 Sandbox client and its two callers

`trader/authored/sandbox_client.py`: opens a session, streams bars, returns
decisions; one session per (strategy, run): research, one per conid; live, one
per deployment for all its conids (14). `SANDBOX_BUSY` and an
unreachable socket are **availability** errors; only `SANDBOX_ERROR` and
`SANDBOX_DIGEST_MISMATCH` are faults of the strategy.

- **Research:** an authored key is evaluated with the SP2c evaluation, but the
  backtester gets a proxy strategy whose `on_bar(prices, state, i)` sends bar `i`
  only and returns the sandbox's decision as a `Signal`. Fill rules, costs, P&L,
  folds, holdout, the determinism check and the shadow replay stay in `research`.
  Each authored file is its own family with its own holdout. A **smoke stage**
  (20 sessions of one research conid) runs first; a `SANDBOX_ERROR` there ends
  the evaluation `REJECTED SANDBOX_ERROR` without spending the holdout or a trial.
  An availability error before the holdout opens returns the record to `STORED`
  (`attempts + 1`; the next night's run takes a new SP2c claim, inside SP2c's
  daily limit); after 3 attempts it is `FAILED SANDBOX_UNAVAILABLE`. After the
  holdout opens, SP2c's rule applies (a holdout that dies still counts as
  revealed).
- **Strategy (live):** each deployed authored strategy holds one live session for
  the trading day, serving all of its conids (14). Each completed bar (from `_strategy_frame`, unclosed
  bars dropped as today) is streamed and a decision maps to a `Signal`. The
  digest is checked at load and on every dispatch, as today. Signals take the
  existing path: signal record, AI intake, Jev, risk gate. An availability error
  live: no signal, an alarm, a reconnect on the next bar; the strategy stays
  `DEPLOYED`. A `SANDBOX_ERROR` live: the strategy becomes `RETIRED
  SANDBOX_ERROR`; open positions keep their stops and close normally.

### 7.7 Trial count across authored strategies

For an authored key, the case field Jev reads, `strategy_trials`
(`trader/ai/research_wire.py`), is the number of authored strategies ever
evaluated to a terminal verdict (all digests) plus this file's own parameter
trials. Catalogue keys keep today's count. SP2c's cooldown stays per strategy
key and is not extended: for authored strategies the growing trial count is the
only brake against writing until one passes by luck, together with the cap and
one file per night.

### 7.8 Scoreboard

A view "AI-written vs catalogue" on the same conids and sessions: daily-P&L
Sharpe, max drawdown, P&L and closed trips for each side. Verdict `yes` only when
the authored side has a higher Sharpe and no worse max drawdown with at least 20
closed trips on each side; `no` when both sides have 20 trips and that fails;
otherwise `not enough data`. Read-only, paper labelled. A report: it never
changes a gate.

### 7.9 Isolation acceptance (staged deploy)

`mmr ai-author prove-isolation` (operator, host only) runs the section 10
"production service" and "hostile session" checks against the **running**
`sandbox` service and records the result in the trader: the sandbox build id
(the image identity, 14), the time, pass or fail, and each check. It runs only
while no live session is open (14). While no passing record exists for the
builds research reported (14), the trader refuses an authored registration
(`AUTHORED_ISOLATION_UNPROVEN`); and the strategy service sends no bar to a
build that has no passing record, whatever the registration said (the binding
check, 14). Evaluation, Jev judgment and SHADOW still run. A rebuilt image needs
a new run. The record holds no secret and no strategy code.

## 8. Failure handling

| Event | Result (state) |
|---|---|
| Author call fails or budget spent | No strategy tonight; recorded. |
| Static check fails twice | Nothing stored; reason recorded. |
| Sandbox unavailable before the holdout | Back to `STORED`, `attempts + 1`; after 3, `FAILED SANDBOX_UNAVAILABLE` (not counted). |
| Sandbox unavailable after the holdout opened | SP2c rule: holdout revealed; evaluation INCOMPLETE, verdict as SP2c. |
| `SANDBOX_ERROR` in evaluation | `REJECTED SANDBOX_ERROR` (not counted). |
| Live `SANDBOX_BUSY` or unreachable | No signal, alarm, reconnect next bar; stays `DEPLOYED`. |
| Live `SANDBOX_ERROR` | `RETIRED SANDBOX_ERROR` (not counted); positions keep stops and close normally. |
| Digest mismatch (store, load or dispatch) | Refused; alarm; live strategy `RETIRED SANDBOX_ERROR`. |
| Trader crash between file write and record | Orphan file only; a later submit of the same digest records it. |
| Switch turned off | No authoring, evaluation or authored entries; closes allowed; states unchanged. |
| No passing isolation record for the running image | Authored DEPLOY verdict recorded, registration refused `AUTHORED_ISOLATION_UNPROVEN`; the record stays `EVALUATING` → `SHADOW` until a run passes, then registers. |
| A child signals or traces the supervisor or another child | `EPERM`; the session ends `SANDBOX_ERROR` on its next bad output; other sessions unaffected. |

## 9. Limits

| Setting | Default | Where |
|---|---|---|
| `authored_strategies.enabled` | false | trader.yaml (owner) |
| `authored_strategies.max_active` | 3 | trader.yaml (owner) |
| `authoring.run_after_close_minutes` | 60 | ai.yaml |
| `authoring.call_timeout_seconds` | 300 | ai.yaml |
| Strategies per night / author retries | 1 / 1 | code |
| File size | 20 KB | code |
| `LOOKBACK_BARS` | 1..2000 | code |
| Per-bar time / child memory | 2 s / address space at fork + 256 MiB | code |
| Live / research sessions | 3 (one per deployment) / 4 (one conid each) | code |
| Conids per live session | 1..20 (SP2c `MAX_CONIDS`) | code |
| `authored_strategies.max_active` range | 1..3 (the live pool) | trader.yaml (owner) |
| Unavailable-sandbox attempts | 3 nights | code |
| `research_service.authored_evaluation_sessions` | 302 (holdout scaled, 14) | trader.yaml (owner) |
| Child uids | 20001..20007 (one per slot) | image |

## 10. Testing

- **Escape corpus** (`tests/authored/escapes/`): each file must be refused by the
  static check and, loaded with the check skipped on purpose, must fail in the
  sandbox. Covers `__subclasses__` walks, frame and generator internals,
  `"{0.__globals__}".format(x)`, `pd.eval` / `DataFrame.query`, pickle,
  `importlib` and `__import__` in strings, computed `getattr`, decorators, lambda
  defaults, `try/except` swallowing a kill.
- **Production service** (full-stack test profile, the real `sandbox` stanza from
  `docker-compose.yml`, not an override): the serving supervisor's effective
  capabilities are exactly SETUID, SETGID and KILL (CHOWN dropped after startup, 14), each child runs as its slot
  uid with no capability and no `sudo`/`trader` group, `network_mode: none`, the
  seccomp profile is loaded; its environment contains no
  `ALPACA_*`, `MASSIVE_*`, `TWELVEDATA_*`, `IB_*` or `OPENROUTER_*` name; `research`
  connects to the research socket and `strategy` to the live socket; a third
  service cannot open either.
- **Benign session** on that service: an ORB port runs 1 bar and 1 full session
  and returns decisions (catches a read-only `HOME` or a failing import).
- **Hostile session** on that service: open a socket, read `/home/trader/.config`
  or keys, write outside its `/tmp/s<slot>`, fork-bomb, allocate past the limit,
  loop forever, and the exact OpenAI path
  `pd.io.common.os.kill(pd.io.common.os.getppid(), 9)` (and `os.kill` of another
  session's child, `os.sys.modules` lookups) loaded with the static check skipped
  on purpose. Each fails (`EPERM` or `SANDBOX_ERROR`); the supervisor and a
  parallel benign session keep running; `PING` still answers.
- **Static check:** the same `pd.io.common.os...` file and every `pd.<module>` /
  `np.<module>` chain are refused `AUTHORED_STATIC_REJECTED`; the shipped
  pandas/numpy walk (7.2) passes.
- **Staged deploy:** with no isolation record for the running build id, an
  authored DEPLOY registers nothing and the load is refused; after a passing
  `prove-isolation` it registers; a different build id needs a new run.
- **Pools:** research holding all 4 research sessions does not delay a live
  `OPEN`; a full pool answers `SANDBOX_BUSY` at once; live `SANDBOX_BUSY` leaves
  the strategy `DEPLOYED` with an alarm.
- **Lookahead by construction:** a strategy that encodes `len(history)` in its
  probability always sees exactly the bars sent, capped at `LOOKBACK_BARS`.
- **Equivalence:** ORB ported to `AuthoredStrategy` gives the same signals bar for
  bar through the sandbox as the catalogue ORB.
- **Cap:** three records left by an unreachable sandbox return to `STORED`, so a
  later night still evaluates; after 3 attempts they are `FAILED` and a new submit
  is accepted; a live `SANDBOX_ERROR` leaves `RETIRED`, which `max_active` does not
  count; a full cap refuses `AUTHORED_CAP_FULL`.
- **Trial count on the case:** after one authored file is evaluated, the next
  file's case, as sent to Jev, has `strategy_trials` ≥ 1 + its own trials.
- **Config:** `ai.yaml` without `roles.author` or with an empty author model loads
  and the ai service starts; `trader.yaml` with `ai_paper.authored_strategies`
  loads; an `AI_PAPER*` override still fails load.
- **Keys:** `check-mount` fails when `ai_author.key` is mounted outside `ai`;
  `submit_authored_strategy` refused for every other principal.
- **Authority:** switch off → `AUTHORED_STRATEGIES_DISABLED`; switch off refuses
  authored entries but allows closes; idempotent resubmit; file before record.
- **Benchmark (first plan task):** time a full evaluation of one authored strategy
  (10 conids, `1 min`, SP2c windows). If it exceeds 4 hours on the owner's
  machine, the plan proposes fewer evaluation sessions for authored strategies
  (owner ruling), never a weaker sandbox.

## 11. Rollout

1. This spec reviewed by OpenAI (correctness, isolation) and Grok (operability,
   tests), then the owner.
2. Implementation plans (#110), one plan per PR, with an index of owner rulings
   and migration numbers.
3. Built and merged with the switch off and the author model empty.
4. Paper acceptance: the owner runs `mmr ai-author prove-isolation` on the
   running stack, sets the author model and turns the switch on; over one night a
   strategy is stored, evaluated and judged; on DEPLOY, live signals flow through
   the sandbox.
5. Runs as part of the paper soak.

## 12. Out of scope

Pairs and multi-symbol strategies, short selling, a `precompute` fast path for
authored strategies, editing an authored file (a change is a new file), live
trading.

## 13. Open questions

- Exact evaluation length for authored strategies if the benchmark (section 10)
  is too slow. Settled in 14: 302 sessions (owner confirmed).

## 14. Amendments (2026-10-11, from the plans)

The implementation plans (index `2026-10-11-ai-paper-sp3-00-index.md`) found these
gaps. The lines above are fixed in place and point here. The owner confirms them
with the plan rulings.

- **Live pool per deployment.** A deployment binds 8 to 20 conids (SP2c); the old
  "3 × 3" counted parameter points. One live session now serves one deployment:
  `OPEN` carries `conids` (1-20), `BAR` and `SIGNAL` carry `conid`. One child
  holds one strategy instance and one history per conid (same digest, so no
  isolation is needed between them); times increase per conid; a fault on any
  conid ends the whole session and retires the strategy. Live pool 3, research
  pool 4 (one conid per session, used one at a time per job). Seven slots, uids
  `20001..20007`. `max_active` is 1..3, so every deployment can get a session.
- **Image identity is a build id.** `docker.sh` bakes `<git sha>-<UTC time>` into
  `/etc/mmr-build-id`; `PONG` reports it as `image_digest`. The isolation record,
  the registration gate and the strategy service's load check all key on it. No
  `docker inspect`: `PONG` also reports the posture (capabilities, seccomp,
  no-new-privileges, network, environment, read-only root, pids limit). The
  trader learns the running build id from research's `STARTED` report.
  Registration needs a passing proof for the build that evaluated the strategy
  and for the build research reported last, checked again inside the seal
  transaction; it does not prove the build running now. The binding check is
  the strategy service's: it refreshes the proof at every reconcile, opens no
  live session on a build without a passing proof (it reads the build from
  `PING` before `OPEN` and from `OPENED`), and checks it on every bar.
- **Isolation run.** `mmr ai-author prove-isolation` runs
  `mmr ai-author prove-isolation --in-container` inside `research` (research
  socket, no key). Hostile files load through `OPEN_PROBE` from the read-only
  image (`trader/sandbox/probes/`, `tests/authored/escapes/`); only probes skip
  the static check. A hostile check passes only if the escape opened, failed at
  `BAR` for its own reason, and a neighbouring session and `PING` still answer.
  The run refuses while a live session is open (`PONG.live_open`, owner ruling),
  so a probe never runs next to a live strategy's child. Each hostile file
  attempts a genuinely forbidden act (write under read-only `/store`, fork past
  the process limit, kill across uids, open a socket on a network-less
  container), so its `BAR` fails for the reason the check expects. The host
  records the printed result as `cli`.
- **Startup CAP_CHOWN.** The supervisor needs CHOWN once, to give each slot its
  own `/tmp/s<uid>` (mode 0700); it drops CHOWN before it serves. The serving
  set stays SETUID, SETGID and KILL. After a session a short helper running as
  the slot uid empties the slot; a slot it cannot empty is never reused.
- **Child memory.** A forked pandas process already maps more than 256 MB, so
  `RLIMIT_AS` is the address space at fork plus 256 MiB.
- **Entries stop at the send.** A retired or switched-off authored strategy is
  also refused on the order saga's SUBMITTING transaction, so a retirement
  between the last gate and the broker send stops the entry. The gate resolves
  the record from the entering version's base deployment, so a renewed version
  is fenced as well.
- **The live proof is never cached.** The strategy service reads the trader's
  latest proof for the sandbox build on every new session and every bar, so a
  proof withdrawn for a build already running stops the next bar (zero stale
  bars).
- **Author spend has its own role.** The trader's `ai_costs.role` accepts
  `author` (journal migration 132 rebuilds the table, rows and seals unchanged).
- **Tunables carry a default.** `submit_authored_strategy` sends
  `{low, high, default}` per tunable; the default must equal the class
  constant. The sandbox accepts any finite number for a tunable (research
  neighbours may leave the range); the trader checks ranges at submit and claim.
- **Evaluation length (benchmark; owner confirmed 2026-10-11).** A full
  authored evaluation at the SP2c defaults is estimated at 10.6 h (measured,
  Plan 1 ruling 1; over 4 h). An authored evaluation uses the most recent 302 sessions
  (`research_service.authored_evaluation_sessions: 302` in `trader.yaml`), with
  SP2c's folds and embargo and the holdout scaled in proportion (39 sessions).
  Catalogue strategies keep the full SP2c length. The sandbox is not weakened.
