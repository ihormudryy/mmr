# AI Paper Bot — SP3: AI-Written Strategies in a Sandbox — Design

Status: design approved section by section by the owner on 2026-10-11; this file is
the written spec for owner review. Tickets: #37 (epic), #109 (this spec), #110
(plans). Builds on SP1 (`2026-10-05-ai-paper-sp1-foundation-design.md`), SP2a+b
(`2026-10-07-ai-paper-sp2ab-autonomous-loop-design.md`) and SP2c
(`2026-10-08-ai-paper-sp2c-backtest-judge-design.md`), all merged.

## 1. Goal

An AI model writes new strategy code. The code runs only in a locked-down sandbox
that can compute signals and nothing else. A written strategy trades on paper only
after the full SP2c gate (evaluation, holdout, Jev DEPLOY, attestation,
registration). SP3 succeeds when an AI-written strategy reaches DEPLOY and its
forward paper P&L beats the catalogue strategies on the same symbols.

## 2. Owner decisions (2026-10-11, do not re-argue)

- **What SP3 proves:** AI strategies from the AI's own ideas, compared with the
  catalogue on forward paper P&L ("beat the catalogue").
- **Author:** a new `author` model role with its own `ai.yaml` entry (any backend).
  Jev stays an independent judge. Spend counts against the same daily budget.
- **Cadence:** at most one new strategy per night, after the close.
- **Cap:** at most 3 AI-written strategies in evaluation, shadow or deployed.
- **Authority:** automatic while the owner switch is on; only Jev DEPLOY plus
  attestation lets one trade. No per-strategy owner approval.
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
| Session (sandbox) | One child process serving one strategy, one conid, one run. |
| Authored key | `authored:sha256:<hex>:<Class>`, the strategy key of an authored file. |

## 4. Findings in today's code that shape this design

- A strategy instance gets `self.storage` (the price database) and, live,
  `self.strategy_runtime` (the whole runtime object). Nothing restricts imports;
  the only "sandbox" today is a path check under `strategies/`.
- The backtester hands `on_bar` the full price frame, future bars included, and
  `assert_no_lookahead` covers only `precompute`. Research backtests run in the
  `research` container, which also holds the research signing key.
- The SP2c binding covers the strategy file bytes, class, params, conids and bar
  size. It does not cover helper modules, so an authored file may import none.

So AI code must never run in the `research`, `strategy`, `trader` or `ai`
processes, and lookahead must be impossible by construction, not caught later.
The catalogue `on_bar` lookahead gap is a separate bug (own ticket), not fixed here.

## 5. Roles and authority

| Unit / principal | May | May not |
|---|---|---|
| `author` role (in `ai`) | Write one file, its tunables and a note per night | Run code, store files, choose symbols outside the menu, judge |
| `ai_author` principal (new) | `submit_authored_strategy` | Every other method |
| `trader` | Static check, write the store, keep the records, offer candidates | Run authored code |
| `research` | Evaluate authored strategies through the sandbox client | Run authored code in process |
| `strategy` | Stream live bars to the sandbox, record signals | Run authored code in process |
| `sandbox` | Run authored code on bars it was sent | Network, keys, config, databases, writes outside its tmpfs |
| Jev | Judge DEPLOY / SHADOW / REJECT as in SP2c | Judge its own code (it writes none) |

Only `ai_author` may call `submit_authored_strategy`; everyone else gets
`PERMISSION_DENIED`. `ai_sandbox` from the SP1 principal table has no key and no
methods; the sandbox speaks no typed RPC at all.

## 6. Owner configuration

In `trader.yaml`, read only from the file (an environment override fails load,
as for `limits_ceiling`):

```yaml
ai_paper:
  authored_strategies:
    enabled: false   # the owner switch
    max_active: 3    # STORED + EVALUATING + SHADOW + DEPLOYED
```

Switch off stops new authoring, new evaluations of authored strategies, and new
entries from deployed authored strategies (`AUTHORED_STRATEGIES_DISABLED`). Closes
always stay allowed. An authored DEPLOY also counts toward SP2c
`max_active_deploys`.

In `ai.yaml`: `roles.author` (`backend`, `model`, token limits) and its price row,
like the other roles. `author.run_after_close_minutes: 60` (after research's own
run).

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
close, volume`; DatetimeIndex). Tunables are upper-case class constants (as
today); `params` carries their values. No storage, runtime, logger, clock,
randomness or conid lookup is available. Single symbol, long only. Jev and the
risk gate size the trade, as for catalogue signals.

### 7.2 Static checker

`trader/authored/static_check.py`, pure code, no execution. Runs at the author,
at the trader (authority) and in the sandbox before load. Refuses with a reason
code (`AUTHORED_STATIC_REJECTED` plus the rule) on:

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
- `global` / `nonlocal`, `async`, `yield`, decorators, `lambda` default-argument
  capture tricks (any lambda with defaults), `try` / `except` (no swallowing the
  sandbox's errors);
- a file over 20 KB, or one that does not parse.

The list is an allow-list for imports and a deny-list for names; section 10 lists
the escape corpus it must refuse.

### 7.3 Store and records (trader)

- Volume `mmr_authored_strategies`. The trader mounts it read-write; `sandbox`,
  `research` and `strategy` mount it read-only; `ai` does not mount it.
- File path `<hex>.py` plus `<hex>.json` (class, tunables, bar size, author note,
  author model, created time). Written with a temp file and atomic rename, file
  before record.
- Table `authored_strategies` in the trader DB: digest (primary key), class,
  state, reason, created, state history. States: `STORED → EVALUATING →
  REJECTED | SHADOW | DEPLOYED → RETIRED | EXPIRED`; `SANDBOX_ERROR` and
  `DISABLED` are reasons on a terminal state.
- `submit_authored_strategy` (`ai_author`): checks the switch, the cap, the
  static check, the bar size (from `research.bar_sizes`), the tunables (numbers
  only, each with a range), then writes the store and the record. A digest that
  already exists returns the existing record (idempotent). Refusals:
  `AUTHORED_STRATEGIES_DISABLED`, `AUTHORED_CAP_FULL`,
  `AUTHORED_STATIC_REJECTED`, `AUTHORED_BAD_TUNABLES`, `AUTHORED_BAD_BAR_SIZE`.

### 7.4 Author loop (ai service)

Runs once per New York trading day at close + 60 minutes when: the switch is on,
the cap has room (read from the trader), the experiment is not KILLED, and the
budget has room. Input, built by code: the API text, the research universe and
allowed bar sizes, catalogue evaluation stats and scoreboard results, and every
earlier authored strategy with its outcome and Jev's reason. Output (schema
checked): file text, tunables with ranges, bar size, a note of at most 600 chars.
The author runs the static check; on a failure it gets the error back and one
retry. A second failure ends the night; the attempt and its reason are recorded in
`ai.duckdb`. Then `submit_authored_strategy`. The `ai` service never reaches the
sandbox or the store. Runtime failures are caught by research's smoke stage
(7.6) and reach the author the next night through the outcome history.

### 7.5 Sandbox service

Compose service `sandbox`:

- `network_mode: none`, `read_only: true`, own user (`sandbox`, not `trader`),
  `cap_drop: [ALL]`, `security_opt: [no-new-privileges:true]`, `pids_limit: 64`,
  1 CPU, 1 GB memory, tmpfs `/tmp` (16 MB, `noexec`).
- Mounts: the store read-only and the socket volume `mmr_sandbox_sock`
  (`/run/sandbox`). Nothing else: no `~/.config`, keys, `.env`, data or logs
  volume (it logs to stdout).
- Same image as the other services, so numpy and pandas versions match.
- Health: the socket answers `PING`.

Protocol on `/run/sandbox/sandbox.sock`, frames of 4-byte length plus JSON,
`"v": 1`:

| Request | Reply |
|---|---|
| `OPEN {digest, class, params, bar_size}` | `OPENED {session}` or `ERROR {code}` |
| `BAR {session, t, open, high, low, close, volume}` | `SIGNAL {session, t, action, probability}` (action may be null) |
| `CLOSE {session}` | `CLOSED` |
| `PING` | `PONG` |

`OPEN` re-hashes the stored file and refuses a mismatch
(`SANDBOX_DIGEST_MISMATCH`), runs the static check, then starts one child process
that loads the class. A session holds only bars it was sent, trimmed to
`LOOKBACK_BARS`; it never receives a future bar. `BAR` times must strictly
increase.

Limits per child: 2 s wall per bar, `RLIMIT_CPU`, `RLIMIT_AS` 512 MB,
`RLIMIT_NPROC` blocking new processes, closed file descriptors except the pipe to
its parent. At most 8 sessions at once; extra `OPEN`s wait (bounded queue, then
`SANDBOX_BUSY`). Inside the child: an import guard that allows only the listed
modules, and reduced built-ins. Any limit hit, exception, bad output (action not
BUY/SELL/null, probability outside 0..1 or NaN) kills the child and returns
`ERROR {code: SANDBOX_ERROR, detail}`; the service keeps serving other sessions.

### 7.6 Sandbox client and its two callers

`trader/authored/sandbox_client.py`: opens a session, streams bars, returns
decisions; one session per (strategy, conid, run).

- **Research:** an authored key is evaluated with the SP2c evaluation, but the
  backtester gets a proxy strategy whose `on_bar(prices, state, i)` sends bar `i`
  only and returns the sandbox's decision as a `Signal`. Fill rules, costs, P&L,
  folds, holdout, determinism check (holdout run twice) and the shadow replay stay
  in `research`. Each authored file is its own family with its own holdout.
  Before the first window, a **smoke stage** runs 20 sessions of one research
  conid; a `SANDBOX_ERROR` there ends the evaluation as REJECTED `SANDBOX_ERROR`
  without spending the holdout or a trial.
- **Strategy (live):** a deployed authored strategy runs as a proxy that streams
  each completed bar (from the same `_strategy_frame`, unclosed bars dropped as
  today) and maps a decision to a `Signal`. The digest is checked at load and on
  every dispatch, as today. Signals take the existing path: signal record, AI
  intake, Jev, risk gate.

### 7.7 Trial count across authored strategies

Jev's trial count for an authored family is the number of authored strategies
ever evaluated (all families), plus the family's own parameter trials. A model
that keeps writing until one passes by luck therefore gets a growing haircut.

### 7.8 Scoreboard

A view "AI-written vs catalogue": forward paper P&L of authored and catalogue
strategies on the same conids and sessions, with a verdict `yes` / `no` /
`not enough data` (fewer than 20 closed trips on either side). Read-only, paper
labelled.

## 8. Failure handling

| Event | Result |
|---|---|
| Author call fails or budget spent | No strategy tonight; recorded. |
| Static check fails twice | Nothing stored; reason recorded. |
| Sandbox unreachable during evaluation | Evaluation INCOMPLETE, retried next night. |
| Sandbox unreachable live | No signal, no entry; alarm; strategy keeps its record. |
| Strategy exception, limit or bad output in evaluation | REJECTED `SANDBOX_ERROR`; slot freed. |
| Same, live | Disabled `SANDBOX_ERROR`; open positions keep their stops and close normally; slot freed. |
| Digest mismatch (store, load or dispatch) | Refused; alarm. |
| Trader crash between file write and record | Orphan file only; a later submit of the same digest records it. |
| Switch turned off | No authoring, evaluation or authored entries; closes allowed. |

## 9. Limits

| Setting | Default | Where |
|---|---|---|
| `authored_strategies.enabled` | false | trader.yaml (owner) |
| `authored_strategies.max_active` | 3 | trader.yaml (owner) |
| Strategies per night | 1 | code |
| Author retries per night | 1 | code |
| File size | 20 KB | code |
| `LOOKBACK_BARS` | 1..2000 | code |
| Per-bar time | 2 s | code |
| Child memory | 512 MB | code |
| Concurrent sessions | 8 | code |

## 10. Testing

- **Escape corpus** (`tests/authored/escapes/`): each file must be refused by the
  static check and, loaded with the check skipped on purpose, must fail in the
  sandbox. Covers: `__subclasses__` walks, frame and generator internals
  (`gi_frame`, `f_globals`), `"{0.__globals__}".format(x)`, `pd.eval` /
  `DataFrame.query`, pickle, `importlib` and `__import__` in strings, computed
  `getattr`, decorators and lambda defaults, `try/except` swallowing a kill.
- **Container tests** (full-stack test profile, real `sandbox` container): a
  hostile strategy tries to open a socket, read `~/.config` and keys, write
  outside `/tmp`, fork-bomb, allocate past the limit and loop forever. Each ends
  in `SANDBOX_ERROR` and a parallel normal session still completes.
- **Lookahead by construction:** a strategy that returns `len(history)` (encoded
  in probability) always sees exactly the bars sent, capped at `LOOKBACK_BARS`.
- **Equivalence:** OpeningRangeBreakout ported to `AuthoredStrategy` gives the
  same signals bar for bar through the sandbox as the catalogue ORB.
- **Authority:** `submit_authored_strategy` refused for every other principal;
  switch off → `AUTHORED_STRATEGIES_DISABLED`; cap full → `AUTHORED_CAP_FULL`;
  switch off refuses authored entries but allows closes; idempotent resubmit.
- **Trial count:** includes every authored strategy ever evaluated.
- **Crash order:** file before record; record without file impossible.
- **Benchmark (first plan task):** time a full evaluation of one authored
  strategy (10 conids, `1 min`, SP2c windows). If it exceeds 4 hours on the
  owner's machine, the plan proposes fewer evaluation sessions for authored
  strategies (owner ruling), never a weaker sandbox.

## 11. Rollout

1. This spec reviewed by OpenAI (correctness, isolation) and Grok (operability,
   tests), then the owner.
2. Implementation plans (#110), one plan per PR, with an index of owner rulings
   and migration numbers.
3. Built and merged with the switch off.
4. Paper acceptance: the owner turns the switch on; over one night a strategy is
   stored, evaluated and judged; on DEPLOY, live signals flow through the sandbox.
5. Runs as part of the paper soak.

## 12. Out of scope

Pairs and multi-symbol strategies, short selling, a `precompute` fast path for
authored strategies, editing an authored file (a change is a new file), the
catalogue `on_bar` lookahead gap (own ticket), live trading.

## 13. Open questions

- Exact evaluation length for authored strategies if the benchmark (section 10)
  is too slow.
